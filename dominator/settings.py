import logging
import os.path
from pkg_resources import resource_stream
import yaml

_logger = logging.getLogger(__name__)


class Settings(dict):
    def load(self, filename):
        if filename is None:
            for filename in ['settings.yaml',
                             os.path.expanduser('~/.config/dominator/settings.yaml'),
                             '/etc/dominator/settings.yaml']:
                _logger.debug("checking existense of %s", filename)
                if os.path.exists(filename):
                    _logger.info("loading settings from %s", filename)
                    stream = open(filename)
                    break
            else:
                _logger.warning("could not find any settings file, using default")
                stream = resource_stream(__name__, 'settings.yaml')
        else:
            stream = open(filename)
        self.update(yaml.load(stream))

settings = Settings()
