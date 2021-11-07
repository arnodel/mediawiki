#!/usr/bin/env python3
# Copyright 2021 Ubuntu
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

"""Charm the service.

Refer to the following post for a quick-start guide that will help you
develop a new k8s charm using the Operator Framework:

    https://discourse.charmhub.io/t/4208
"""

import logging
from dataclasses import dataclass
from subprocess import check_call, CalledProcessError
import os
import secrets
from contextlib import contextmanager
from enum import Enum

from jinja2 import Environment, PackageLoader
import pymysql

from ops.charm import CharmBase, ConfigChangedEvent, InstallEvent, RelationChangedEvent, RelationDepartedEvent, RelationJoinedEvent
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus

logger = logging.getLogger(__name__)

# Templates go in the "src/templates".  Get them with
# `templates.get_template(filename)`.
templates =  Environment(loader=PackageLoader("src"))

# Where to find the mediawiki install php script
INSTALL_PHP = "/usr/share/mediawiki/maintenance/install.php"

# Where to put the mediawiki config files
MEDIAWIKI_CONFIG_DIR = "/etc/mediawiki"

# Path of the config.php file containing the configuration generated from the
# charm config.
CONFIG_PHP_PATH = f"{MEDIAWIKI_CONFIG_DIR}/config.php"


class DbStatus(Enum):
    '''
    Status of the connection to the database
    '''
    DISCONNECTED = 1
    JOINED = 2
    CONNECTED = 3


class MediawikiCharm(CharmBase):
    """Charm the service."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.db_relation_joined, self._on_db_relation_joined)
        self.framework.observe(self.on.db_relation_changed, self._on_db_relation_changed)
        self.framework.observe(self.on.db_relation_departed, self._on_db_relation_departed)
    
        # Keep track of the database connection status in order to report
        # correct unit statuses.
        self._stored.set_default(db_status=DbStatus.DISCONNECTED)

    def _on_install(self, event: InstallEvent) -> None:
        self.unit.status = MaintenanceStatus("Installing packages")
        try:
            install_mediawiki_packages()
            self.unit.status = MaintenanceStatus("Packages installed")
        except CalledProcessError as e:
            logger.debug("Package install failed with return code", e.returncode)
            self.unit.status = BlockedStatus("Failed to install packages")

    def _on_config_changed(self, event: ConfigChangedEvent):
        self.unit.status = MaintenanceStatus("Configuring Mediawiki")
        try:
            configure_mediawiki(self.config)
            reload_apache()
            self._set_installed_unit_status()
        except Exception as e:
            logger.debug("Error configuring mediawiki: %s", e)
            self.unit.status = BlockedStatus("Failed to configure mediawiki")


    def _on_db_relation_joined(self, event: RelationJoinedEvent) -> None:
        self._stored.db_status = DbStatus.JOINED
        self._set_installed_unit_status()
    
    def _on_db_relation_changed(self, event: RelationChangedEvent) -> None:
        db = event.relation.data[event.unit]
        with database_lock(db):
            # The mediawiki install script run below attempts to create all the
            # database tables, skips otherwise. The lock context ensures that
            # either all the tables are created or none are when the script
            # runs.
            try:
                install_mediawiki(db)
                reload_apache()
                self._stored.db_status = DbStatus.CONNECTED
                self._set_installed_unit_status()
            except Exception as e:
                logger.debug("Mediawiki install failed with error: %s", e)
                self.unit.status = BlockedStatus("Failed to install mediawiki")

    def _on_db_relation_departed(self, event: RelationDepartedEvent) -> None:
        self._stored.db_status = DbStatus.DISCONNECTED
        self._set_installed_unit_status()
        try:
            uninstall_mediawiki()
        except Exception as e:
            logger.debug("Uninstalling failed with error %s", e)
    
    def _set_installed_unit_status(self):
        self.unit.status = unit_status_from_db_status[self._stored.db_status]


unit_status_from_db_status = {
    DbStatus.DISCONNECTED: BlockedStatus("Waiting for database"),
    DbStatus.JOINED: MaintenanceStatus("Connecting to database"),
    DbStatus.CONNECTED: ActiveStatus("Ready")
}


#
# Helper functions
#


def install_mediawiki_packages():
    '''
    Set up the necessary packages for mediawiki to run
    '''
    check_call(["apt-get", "install", "-y", "mediawiki", "imagemagick"])
 

def install_mediawiki(db):
    '''
    Create the wiki database tables and the basic LocalSettings.php file
    '''
    # Call the mediawiki install script that creates the database tables if
    # necessary, creates an admin user and generates a LocalSettings.php file.
    # The fact that it does all these things in one go is a challenge!
    check_call(["php", "/usr/share/mediawiki/maintenance/install.php",
        "--dbserver", db["private-address"],
        "--dbname", db["database"],
        "--dbuser", db["user"],
        "--dbpass", db["password"],
        "--confpath", MEDIAWIKI_CONFIG_DIR,
        "--installdbuser", db["user"],
        "--installdbpass", db["password"],
        "--pass", secrets.token_urlsafe(32),
        "Charmed Wiki",
        "generic_charm_admin"
    ])

    # Make sure the config.php file exists, as LocalSettings will include it.
    with open(CONFIG_PHP_PATH, "a"):
        pass
    os.chmod(CONFIG_PHP_PATH, 0o644)

    # Include the config.php file in LocalSettings.  When configuration changes,
    # only that file needs to be regenerated.
    with open(f"{MEDIAWIKI_CONFIG_DIR}/LocalSettings.php", "a") as f:
        f.write("\n")
        f.write(f"include('{CONFIG_PHP_PATH}')")


def uninstall_mediawiki():
    '''
    Remove the LocalSettings.php file,  returning mediawiki to its uninstalled
    state.
    '''
    os.remove(f"{MEDIAWIKI_CONFIG_DIR}/LocalSettings.php")


def configure_mediawiki(conf):
    '''
    Overwrite the config.php file containing the mediawiki config that comes
    from the charm config.
    '''
    template = templates.get_template("config.php")
    config_php = template.render(
        wiki_name=conf["name"],
        language_code=conf["language"],
        skin=conf["skin"],
        server_address=conf["server_address"],
        logo="",     # TODO
        debug_file="" if not conf["debug"] else os.getcwd() + "/debug.log",
    )
    with open(CONFIG_PHP_PATH, "w") as f:
        f.write(config_php)
    os.chmod(CONFIG_PHP_PATH, 0o644)


def create_or_update_admin(username: str, pwd: str):
        check_call(["php", f"{MEDIAWIKI_MAINTENANCE_ROOT}/createAndPromote.php",
        "--conf", f"{MEDIAWIKI_CONFIG_DIR}/LocalSettings.php",
        "--force",
        "--sysop", "--bureaucrat",
        username, pwd,
        ])


def reload_apache():
    check_call(["service", "apache2", "reload"])


@contextmanager
def database_lock(db):
    '''
    Acquire a lock to the wiki database for the scope of the context.
    '''
    connection = pymysql.connect(
        host=db["private-address"],
        user=db["user"],
        password=db["password"],
        database=db["database"],
    )
    # The idea is to have a table (charm_lock) and acquire a WRITE lock on it.
    # Only one WRITE lock can be held on a table at a time.
    with connection.cursor() as cursor:
        cursor.execute("CREATE TABLE IF NOT EXISTS charm_lock(n int)")
        cursor.execute("LOCK TABLES charm_lock WRITE")
        try:
            yield
        finally:
            cursor.execute("UNLOCK TABLES")
            connection.close()


if __name__ == "__main__":
    main(MediawikiCharm)
