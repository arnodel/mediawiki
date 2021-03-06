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
import tempfile

from jinja2 import Environment, FileSystemLoader

from ops.charm import (
    CharmBase, ConfigChangedEvent, InstallEvent, RelationChangedEvent, RelationCreatedEvent,
    RelationDepartedEvent, RelationJoinedEvent, StartEvent,
)
from ops.framework import StoredState
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, WaitingStatus

logger = logging.getLogger(__name__)

# Templates go in the "src/templates".  Get them with
# `templates.get_template(filename)`.
templates = Environment(loader=FileSystemLoader("src/templates"))

# Where to find the mediawiki maintenance php scripts
MEDIAWIKI_MAINTENANCE_ROOT = "/usr/share/mediawiki/maintenance"

# Where to put the mediawiki config files
MEDIAWIKI_CONFIG_DIR = "/etc/mediawiki"

# Root of the mediawiki installation
MEDIAWIKI_ROOT_DIR = "/var/lib/mediawiki"

# Path of the config.php file containing the configuration generated from the
# charm config.
CONFIG_PHP_PATH = f"{MEDIAWIKI_CONFIG_DIR}/config.php"

# Path of the memcached.php file containing the configuration generated from a
# memcached relation.
MEMCACHED_PHP_PATH = f"{MEDIAWIKI_CONFIG_DIR}/memcached.php"

# Path of the db.php file containing configuration generated from a db relation
DB_PHP_PATH = f"{MEDIAWIKI_CONFIG_DIR}/db.php"

# Path of the main mediawiki configuration, which is generated by the
# install.php maintenance script once the database connection details are known.
# Include directives are added to also load the scripts above.
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

        # self.framework.observe(self.on.replicas_relation_joined,
        #                        self._on_replicas_relation_changed)
        self.framework.observe(self.on.replicas_relation_changed,
                               self._on_replicas_relation_changed)

        self.framework.observe(self.on.cache_relation_changed, self._on_cache_relation_changed)
        self.framework.observe(self.on.cache_relation_departed, self._on_cache_relation_departed)

        self.framework.observe(self.on.website_relation_joined, self._on_website_relation_joined)

    # Lifecycle hooks

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
        db = event.relation.data[event.unit]
        configure_db(db)
        if self.unit.is_leader():
            self._install_mediawiki(db)

    def _on_db_relation_departed(self, event: RelationDepartedEvent) -> None:
        self._set_db_connection_status(False)
        self.unit.status = BlockedStatus("Missing db relation")
        try:
            uninstall_mediawiki()
            reload_apache()
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

    # All units configure themseleves to use memcached, making use off all
    # memcached units.

    def _on_cache_relation_changed(self, event: RelationChangedEvent) -> None:
        try:
            servers = []
            for unit in event.relation.units:
                unit_data = event.relation.data[unit]
                if "private-address" in unit_data and "port" in unit_data:
                    servers.append({
                        "address": unit_data["private-address"],
                        "port": unit_data["port"],
                    })
            configure_memcached(servers)
            reload_apache()
        except Exception as e:
            logger.error("Failed to configure memcached: %s", e)
            self.unit.status = BlockedStatus("Memcached configuration failed")

    def _on_cache_relation_departed(self, event: RelationDepartedEvent) -> None:
        try:
            configure_memcached(None)
            reload_apache()
        except Exception as e:
            logger.error("Unable to remove memcached configuration: %s", e)
            self.unit.status = BlockedStatus("Memcached removal failed")

    # The website relation implements providing the http interface, so it must
    # expose a port and a hostname to consumers of this interface.

    def _on_website_relation_joined(self, event: RelationJoinedEvent) -> None:
        unit_data = event.relation.data[self.unit]
        ingress_address = self.model.get_binding('website').network.ingress_address
        unit_data.update({
            "port": "80",
            "hostname": str(ingress_address),
        })

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
        app_data = self.model.get_relation("replicas").data[self.app]
        app_data.update({
            "status": "connected" if connected else "disconnected",
            "revision": str(int(app_data.get("revision", 0)) + 1),
        })

    def _get_db_connection_status(self) -> bool:
        app_data = self.model.get_relation("replicas").data[self.app]
        return app_data.get("status") == "connected"


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
    check_call([
        "sed", "-i",
        f"s|DocumentRoot .*|DocumentRoot {MEDIAWIKI_ROOT_DIR}|",
        "/etc/apache2/sites-available/000-default.conf",
    ])


def are_mediawiki_packages_installed():
    try:
        check_call([
            "grep", "-q",
            f"DocumentRoot {MEDIAWIKI_ROOT_DIR}",
            "/etc/apache2/sites-available/000-default.conf",
        ])
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

    if is_mediawiki_installed():
        configure_db(db)
        return

    with tempfile.TemporaryDirectory() as temp_dir:
        check_call([
            "php", f"{MEDIAWIKI_MAINTENANCE_ROOT}/install.php",
            "--dbserver", db["private-address"],
            "--dbname", db["database"],
            "--dbuser", db["user"],
            "--dbpass", db["password"],
            "--confpath", temp_dir,
            "--installdbuser", db["user"],
            "--installdbpass", db["password"],
            "--pass", secrets.token_urlsafe(32),
            "--scriptpath", "",
            "Charmed Wiki",
            "generic_charm_admin"
        ])

        # Include the config php files in LocalSettings.  When configuration
        # changes, only that file needs to be regenerated.
        with open(f"{temp_dir}/LocalSettings.php", "a") as f:
            f.write("\n")
            f.write(f"include('{CONFIG_PHP_PATH}');\n")
            f.write(f"include('{MEMCACHED_PHP_PATH}');\n")
            f.write(f"include('{DB_PHP_PATH}');\n")

        # Make sure the config php files exists, as LocalSettings
        # will include them.
        touch_config(CONFIG_PHP_PATH)
        touch_config(MEMCACHED_PHP_PATH)
        touch_config(DB_PHP_PATH)

        # Finally swap in the configuration
        os.rename(f"{temp_dir}/LocalSettings.php", LOCALSETTINGS_PHP_PATH)


def configure_db(db):
    '''
    Regenerate the db.php file with database connection data
    '''
    template = templates.get_template("db.php")
    db_php = template.render(db=db)
    write_config(DB_PHP_PATH, db_php)


def is_mediawiki_installed():
    return os.path.exists(LOCALSETTINGS_PHP_PATH)


def uninstall_mediawiki():
    '''
    Remove the LocalSettings.php file,  returning mediawiki to its uninstalled
    state.
    '''
    for f in LOCALSETTINGS_PHP_PATH, CONFIG_PHP_PATH, MEMCACHED_PHP_PATH, DB_PHP_PATH:
        try:
            os.remove(f)
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
    write_config(CONFIG_PHP_PATH, config_php)


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
    '''
    Parse a string of the form "<name1>:<pwd1> <name2>:<pwd2> ..." into
    a list of pairs [["<name1>", "<pwd1>"], ["<name2>", "<pwd2>"], ...]

    Raise a ValueError if the string is not of the correct format.
    '''
    name_pwd_pairs = []
    for item in admins.split():
        if ":" not in item:
            raise ValueError("admin should be in format user:pass")
        name_pwd_pairs.append(item.split(":", 1))
    return name_pwd_pairs


def create_or_update_admin(username: str, pwd: str):
    '''
    Make sure the specified user exists, has the given password and is an
    "admin", i.e. belongs to the sysop and bureaucrat groups.
    '''
    check_call([
        "php", f"{MEDIAWIKI_MAINTENANCE_ROOT}/createAndPromote.php",
        "--conf", LOCALSETTINGS_PHP_PATH,
        "--force",
        "--sysop", "--bureaucrat",
        username, pwd,
    ])


def configure_memcached(servers):
    '''
    Update the memcached configuration with the given servers.  If servers is
    falsy, instead remove memcached configuration.
    '''
    if not servers:
        memcached_php = ""
    else:
        template = templates.get_template("memcached.php")
        memcached_php = template.render(servers=servers)
    write_config(MEMCACHED_PHP_PATH, memcached_php)


def reload_apache():
    check_call(["service", "apache2", "reload"])


def touch_config(path):
    '''
    Ensure a file exists (potentially creating an empty file) with suitable
    permissions for being read as config for mediawiki.
    '''
    with open(path, "a"):
        pass
    os.chmod(path, 0o644)


def write_config(path: str, content: str):
    '''
    Write a file to disk with suitable permissions for being read as config for
    mediawiki.
    '''
    with open(path, "w") as f:
        f.write(content)
    os.chmod(path, 0o644)


if __name__ == "__main__":
    main(MediawikiCharm)
