# Copyright 2021 Ubuntu
# See LICENSE file for licensing details.
#
# Learn more about testing at: https://juju.is/docs/sdk/testing

from subprocess import CalledProcessError
import unittest
from unittest.mock import patch

import charm
from charm import MediawikiCharm
from ops.model import BlockedStatus, WaitingStatus
from ops.testing import Harness


class TestCharm(unittest.TestCase):
    def setUp(self):
        self.harness = Harness(MediawikiCharm)
        self.addCleanup(self.harness.cleanup)
        self.harness.begin()

    def check_status(self, expected_status):
        self.assertEqual(
            self.harness.charm.unit.status,
            expected_status,
        )

    @patch('charm.install_mediawiki_packages')
    def test_install_succeeds(self, *unused):
        self.harness.charm.on.install.emit()
        charm.install_mediawiki_packages.assert_called_once()
        self.check_status(WaitingStatus('Mediawiki packages installed'))

    @patch('charm.install_mediawiki_packages')
    def test_install_fails(self, *unused):
        charm.install_mediawiki_packages.side_effect = CalledProcessError(1, 'foo')
        self.harness.charm.on.install.emit()
        charm.install_mediawiki_packages.assert_called_once()
        self.check_status(BlockedStatus('Failed to install packages'))

    @patch('charm.configure_mediawiki')
    @patch('charm.reload_apache')
    def test_config_changed_succeeds(self, *unused):
        with patch.object(self.harness.charm, '_get_db_relation_status') as mock_get_status:
            mock_get_status.return_value = WaitingStatus('foo')  # needs to be a valid status
            self.harness.update_config({"name": "My Wiki"})
            charm.configure_mediawiki.assert_called_once()
            charm.reload_apache.assert_called_once()
            mock_get_status.assert_called_once()
            self.assertEqual(self.harness.charm.unit.status, mock_get_status.return_value)

    @patch('charm.configure_mediawiki')
    @patch('charm.reload_apache')
    def test_config_changed_fails(self, *unused):
        charm.configure_mediawiki.side_effect = Exception("foo")
        self.harness.update_config({"name": "My Wiki"})
        charm.configure_mediawiki.assert_called_once()
        charm.reload_apache.assert_not_called()
        self.check_status(BlockedStatus("Failed to configure mediawiki"))

    @patch('charm.configure_db')
    def test_db_relation_changed_non_leader(self, *unused):
        with patch.object(self.harness.charm, "_install_mediawiki") as mock_install_mediawiki:
            rel_id = self.harness.add_relation("db", "mysql")
            self.harness.add_relation_unit(rel_id, "mysql/0")
            db_data = {"key": "val"}
            self.harness.update_relation_data(rel_id, "mysql/0", db_data)
            charm.configure_db.assert_called_once_with(db_data)
            mock_install_mediawiki.assert_not_called()

    @patch('charm.configure_db')
    def test_db_relation_changed_leader(self, *unused):
        self.harness.set_leader()
        with patch.object(self.harness.charm, "_install_mediawiki") as mock_install_mediawiki:
            rel_id = self.harness.add_relation("db", "mysql")
            self.harness.add_relation_unit(rel_id, "mysql/0")
            db_data = {"key": "val"}
            self.harness.update_relation_data(rel_id, "mysql/0", db_data)
            charm.configure_db.assert_called_once_with(db_data)
            mock_install_mediawiki.assert_called_once_with(db_data)
