import logging
import os.path
from pkg_resources import Requirement, resource_filename
import yaml

_logger = logging.getLogger(__name__)


class Settings(dict):
    def load(self, filename):
        if filename is None:
            for filename in ['settings.yaml',
                             os.path.expanduser('~/.config/dominator/settings.yaml'),
                             '/etc/dominator/settings.yaml',
                             resource_filename(Requirement.parse('dominator'), 'dominator/settings.yaml')]:
                _logger.debug("checking existense of %s", filename)
                if os.path.exists(filename):
                    break
            else:
                _logger.warning("could not find any settings file")
                return
        _logger.info("loading settings from %s", filename)
        self.update(yaml.load(open(filename)))

settings = Settings()
