from pkg_resources import get_distribution, DistributionNotFound
from .entities import Ship, LocalShip, Container, DataVolume, ConfigVolume, TextFile, TemplateFile
from .utils import ships_from_conductor, ships_from_nova, datacenter_from_racktables, aslist, groupby
from .__main__ import main  # NOQA: this is needed for dominator script to work

__all__ = ['Ship', 'LocalShip', 'Container', 'DataVolume', 'ConfigVolume', 'TextFile', 'TemplateFile',
           'ships_from_conductor', 'ships_from_nova', 'datacenter_from_racktables', 'aslist', 'groupby']

__project__ = 'dominator'
__version__ = None  # required for initial installation

try:
    __version__ = get_distribution(__project__).version
except DistributionNotFound:
    VERSION = __project__ + '-' + '(local)'
else:
    VERSION = __project__ + '-' + __version__
