from pkg_resources import get_distribution, DistributionNotFound
from .entities import *  # NOQA
from .utils import *  # NOQA
from .__main__ import main  # NOQA: this is needed for dominator script to work

__all__ = ['Ship', 'LocalShip', 'Container', 'DataVolume', 'ConfigVolume', 'TextFile', 'TemplateFile',
           'ships_from_conductor']

__project__ = 'dominator'
__version__ = None  # required for initial installation

try:
    __version__ = get_distribution(__project__).version
except DistributionNotFound:
    VERSION = __project__ + '-' + '(local)'
else:
    VERSION = __project__ + '-' + __version__
