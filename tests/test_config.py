import os
import unittest
from unittest.mock import patch
from bot import Config


class ConfigTests(unittest.TestCase):
    def load(self, environment, file_values):
        with patch.dict(os.environ, environment, clear=True), patch('bot.dotenv_values', return_value=file_values) as read:
            config = Config.load(require_secrets=False)
            return config, read.call_args.args[0].name

    def test_local_default(self):
        config, filename = self.load({}, {'FPL_LEAGUE_ID': '1', 'TELEGRAM_BOT_TOKEN': 'local'})
        self.assertEqual((filename, config.token, config.database), ('.env.local', 'local', 'fpl_local.db'))

    def test_production_existing_database_and_file(self):
        config, filename = self.load({'FPL_ENV': 'production', 'DATABASE_PATH': '/var/lib/fpl-bot/fpl_data.db'}, {'FPL_LEAGUE_ID': '1', 'TELEGRAM_BOT_TOKEN': 'production'})
        self.assertEqual((filename, config.token, config.database), ('.env', 'production', '/var/lib/fpl-bot/fpl_data.db'))

    def test_local_missing_token_does_not_load_production(self):
        with patch.dict(os.environ, {}, clear=True), patch('bot.dotenv_values', return_value={'FPL_LEAGUE_ID': '1'}) as read:
            with self.assertRaisesRegex(ValueError, r'Missing environment variables in .env.local'):
                Config.load()
            read.assert_called_once()

    def test_invalid_environment(self):
        with patch.dict(os.environ, {'FPL_ENV': 'typo'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'FPL_ENV must'):
                Config.load()
