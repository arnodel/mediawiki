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
from subprocess import check_call, CalledProcessError
import os
import secrets
import urllib.request
import shutil
import imghdr

from jinja2 import Environment, FileSystemLoader

from ops.charm import CharmBase, ConfigChangedEvent, InstallEvent, RelationChangedEvent, RelationCreatedEvent, RelationDepartedEvent, RelationJoinedEvent, StartEvent
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus

logger = logging.getLogger(__name__)

# Templates go in the "src/templates".  Get them with
# `templates.get_template(filename)`.
templates =  Environment(loader=FileSystemLoader("src/templates"))

# Where to find the mediawiki maintenance php scripts
MEDIAWIKI_MAINTENANCE_ROOT = "/usr/share/mediawiki/maintenance"

# Where to put the mediawiki config files
MEDIAWIKI_CONFIG_DIR = "/etc/mediawiki"

# Root of the mediawiki installation
MEDIAWIKI_ROOT_DIR = "/var/lib/mediawiki"

# Path of the config.php file containing the configuration generated from the
# charm config.
CONFIG_PHP_PATH = f"{MEDIAWIKI_CONFIG_DIR}/config.php"

LOCALSETTINGS_PHP_PATH = f"{MEDIAWIKI_CONFIG_DIR}/LocalSettings.php"


class MediawikiCharm(CharmBase):
    """Charm the service."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.start, self._on_start)
        self.framework.observe(self.on.config_changed, self._on_config_changed)

        self.framework.observe(self.on.db_relation_created, self._on_db_relation_created)
        self.framework.observe(self.on.db_relation_joined, self._on_db_relation_joined)
        self.framework.observe(self.on.db_relation_changed, self._on_db_relation_changed)
        self.framework.observe(self.on.db_relation_departed, self._on_db_relation_departed)

        self.framework.observe(self.on.replicas_relation_changed, self._on_replicas_relation_changed)    

    def _on_install(self, event: InstallEvent) -> None:
        self.unit.status = MaintenanceStatus("Installing mediawiki packages")
        try:
            install_mediawiki_packages()
            self.unit.status = WaitingStatus("Mediawiki packages installed")
        except CalledProcessError as e:
            logger.error("Package install failed with error: %s", e)
            self.unit.status = BlockedStatus("Failed to install packages")

    def _on_start(self, event: StartEvent) -> None:
        check_call(["open-port", "80"])
        self.unit.status = self._get_db_relation_status()

    def _on_config_changed(self, event: ConfigChangedEvent):
        self.unit.status = MaintenanceStatus("Updating Mediawiki configuration")
        try:
            configure_mediawiki(self.config)
            if self.unit.is_leader() and self.config["admins"]:
                setup_admins(self.config["admins"])
            reload_apache()
            self.unit.status = self._get_db_relation_status()
        except Exception as e:
            logger.error("Error configuring mediawiki: %s", e)
            self.unit.status = BlockedStatus("Failed to configure mediawiki")

    # Leader units do operations that affect the database, so only they react to
    # db_relation_changed events.  When they have created the database tables
    # successfully, they trigger an event on the peer relation "replicas" by
    # setting the "connected" key to True, thus indicating to the other units
    # that they can also install mediawiki.

    def _on_db_relation_created(self, event: RelationCreatedEvent) -> None:
        self.unit.status = WaitingStatus("Waiting to join db relation")
        
    def _on_db_relation_joined(self, event: RelationJoinedEvent) -> None:
        self.unit.status = WaitingStatus("Waiting for connection data from db relation")
    
    def _on_db_relation_changed(self, event: RelationChangedEvent) -> None:
        if not self.unit.is_leader():
            return
        db = event.relation.data[event.unit]
        self._install_mediawiki(db)

    def _on_db_relation_departed(self, event: RelationDepartedEvent) -> None:
        self._set_db_connection_status(False)
        self.unit.status = BlockedStatus("Missing db relation")
        try:
            uninstall_mediawiki()
        except Exception as e:
            logger.error("Uninstalling failed with error %s", e)
 
    # Only non-leader units react to replicas_relation_changed.  It is a signal
    # from the leader unit that the mediawiki tables have been installed so they
    # can safely run the installation script without a risk of race.

    def _on_replicas_relation_changed(self, event: RelationChangedEvent) -> None:
        if self.unit.is_leader():
            return
        conf = event.relation.data[event.app]
        if conf.get("status") != "connected":
            # There should have been a db_relation_departed event that
            # triggered the uninstallation, so there is nothing to do in this
            # case.
            return
        db = self._get_db()
        if not db:
            logger.debug("No db connection data found even though the database is connected")
            return
        self._install_mediawiki(db)

    # Methods that help event hooks

    def _get_db(self):
        '''
        Get db connection data from the relation if it exists
        '''
        db_rel = self.model.get_relation("db")
        if db_rel is None:
            return
        db_app_pfx = db_rel.app.name + "/"
        for u in db_rel.units:
            if u.name.startswith(db_app_pfx):
                db = db_rel.data[u]
                if db["slave"] == "False" and "database" in db:
                    return db

    def _get_db_relation_status(self):
        db_rel = self.model.get_relation("db")
        if db_rel is None:
            return BlockedStatus("Missing db relation")
        db = self._get_db()
        if db is None:
            return WaitingStatus("Waiting for connection data from db relation")
        if is_mediawiki_installed():
            return ActiveStatus()
        return WaitingStatus("Waiting to install Mediawiki")        

    def _install_mediawiki(self, db):
        try:
            self.unit.status = MaintenanceStatus("Updating mediawiki db configuration")
            install_mediawiki(db)
            reload_apache()
            self._set_db_connection_status(True)
            self.unit.status = ActiveStatus()
        except Exception as e:
            logger.error("Mediawiki install failed with error: %s", e)
            self.unit.status = BlockedStatus("Failed to install mediawiki")

    def _uninstall_mediawiki(self):
        self._set_db_connection_status(False)
        self.unit.status = BlockedStatus("Missing db relation")
        try:
            uninstall_mediawiki()
        except Exception as e:
            logger.error("Uninstalling failed with error %s", e)
 
    def _set_db_connection_status(self, connected: bool) -> None:
        if not self.unit.is_leader():
            return
        self.model.get_relation("replicas").data[self.app]["status"] = "connected" if connected else "disconnected"


#
# Helper functions
#


def install_mediawiki_packages():
    '''
    Set up the necessary packages for mediawiki to run
    '''
    # Install mediawiki (which pulls php as a dependency) and imagemagick which
    # allows mediawiki to perform image manipulation.
    check_call(["apt-get", "install", "-y", "mediawiki", "imagemagick"])

    # Apache2 is configured by default to serve from /var/www/html.  We replace
    # the DocumentRoot directive in the apache default configuration to point at
    # the mediawiki root.
    check_call(["sed",  "-i", 
        f"s|DocumentRoot .*|DocumentRoot {MEDIAWIKI_ROOT_DIR}|", 
        "/etc/apache2/sites-available/000-default.conf",
    ])


def are_mediawiki_packages_installed():
    try:
        check_call(["grep", "-q", f"DocumentRoot {MEDIAWIKI_ROOT_DIR}", "/etc/apache2/sites-available/000-default.conf"])
        return True
    except CalledProcessError:
        return False


def install_mediawiki(db):
    '''
    Create the wiki database tables and the basic LocalSettings.php file
    '''
    # Call the mediawiki install script that creates the database tables if
    # necessary, creates an admin user and generates a LocalSettings.php file.
    # The fact that it does all these things in one go is a challenge!
    check_call(["php", f"{MEDIAWIKI_MAINTENANCE_ROOT}/install.php",
        "--dbserver", db["private-address"],
        "--dbname", db["database"],
        "--dbuser", db["user"],
        "--dbpass", db["password"],
        "--confpath", MEDIAWIKI_CONFIG_DIR,
        "--installdbuser", db["user"],
        "--installdbpass", db["password"],
        "--pass", secrets.token_urlsafe(32),
        "--scriptpath", "",
        "Charmed Wiki",
        "generic_charm_admin"
    ])

    # Make sure the config.php file exists, as LocalSettings will include it.
    with open(CONFIG_PHP_PATH, "a"):
        pass
    os.chmod(CONFIG_PHP_PATH, 0o644)

    # Include the config.php file in LocalSettings.  When configuration changes,
    # only that file needs to be regenerated.
    with open(LOCALSETTINGS_PHP_PATH, "a") as f:
        f.write("\n")
        f.write(f"include('{CONFIG_PHP_PATH}');")


def is_mediawiki_installed():
    return os.path.exists(LOCALSETTINGS_PHP_PATH)


def uninstall_mediawiki():
    '''
    Remove the LocalSettings.php file,  returning mediawiki to its uninstalled
    state.
    '''
    try:
        os.remove(LOCALSETTINGS_PHP_PATH)
    except FileNotFoundError:
        pass


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
        logo_path=fetch_logo(conf["logo"]),
        debug_file="" if not conf["debug"] else os.getcwd() + "/debug.log",
    )
    with open(CONFIG_PHP_PATH, "w") as f:
        f.write(config_php)
    os.chmod(CONFIG_PHP_PATH, 0o644)
    

def fetch_logo(logo_url) -> str:
    '''
    Fetch the wiki logo from the given URL if necessary, returning the file path
    where it has been stored
    '''

    if not logo_url:
        return

    url_logo_path = "/images/wiki_logo"
    fs_logo_path = f"{MEDIAWIKI_ROOT_DIR}{url_logo_path}"
    logo_src_path = f"{MEDIAWIKI_CONFIG_DIR}/logo_url"

    # Check for an already downloaded this image and return early if that's the
    # case
    try:
        with open(logo_src_path) as f:
            previous_url = f.read()
    except FileNotFoundError:
        previous_url = ""
    if logo_url == previous_url:
        return url_logo_path
    
    # Fetch the image and store it
    with urllib.request.urlopen(logo_url) as response:
        content = response.read()
        ext = imghdr.what(response, content)
    if ext is None:
        raise ValueError("logo is not an image")
    with open(fs_logo_path, "wb") as f:
        f.write(content)
    
    # Remember we've done that
    with open(logo_src_path, "w") as f:
        f.write(logo_url)

    shutil.chown(fs_logo_path, user="www-data", group="www-data")
    return url_logo_path


def setup_admins(admins: str):
    '''
    Make sure the given admin users are setup.

    TODO: remove other admins?
    '''
    for username, pwd in parse_admins(admins):
        create_or_update_admin(username, pwd)


def parse_admins(admins: str):
    name_pwd_pairs = []
    for item in admins.split():
        if ":" not in item:
            raise ValueError("admin should be in format user:pass")
        name_pwd_pairs.append(item.split(":", 1))
    return name_pwd_pairs


def create_or_update_admin(username: str, pwd: str):
        check_call(["php", f"{MEDIAWIKI_MAINTENANCE_ROOT}/createAndPromote.php",
        "--conf", LOCALSETTINGS_PHP_PATH,
        "--force",
        "--sysop", "--bureaucrat",
        username, pwd,
        ])


def reload_apache():
    check_call(["service", "apache2", "reload"])


if __name__ == "__main__":
    main(MediawikiCharm)
