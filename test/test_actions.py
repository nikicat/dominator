import tempfile
import logging
import re
import datetime

import pytest
from vcr import VCR
from colorama import Fore

from dominator.entities import LocalShip, Container, Image, Shipment
from dominator.actions import dump, localstatus, load_from_yaml, localstart, makedeb
from dominator.utils import settings as _settings


vcr = VCR(cassette_library_dir='test/fixtures/vcr_cassettes')


@pytest.yield_fixture(autouse=True)
def settings():
    with tempfile.TemporaryDirectory() as configdir:
        _settings['configvolumedir'] = configdir
        # FIXME: use https endpoint because vcrpy doesn't handle UnixHTTPConnection
        _settings['dockerurl'] = 'http://localhost:4243'
        _settings['deploy-image'] = 'nikicat/dominator'
        yield _settings


@pytest.fixture(autouse=True)
def logs():
    logging.disable(level=logging.DEBUG-1)


@pytest.fixture
def ships():
    return [LocalShip()]


class MockShipment(Shipment):
    def __init__(self, containers):
        self.containers = containers
        self._ships = self.ships = list({container.ship for container in containers})


@pytest.fixture
def shipment():
    return MockShipment([
        Container(
            name='testcont',
            ship=ship,
            image=Image('busybox'),
            command='sleep 10')
        for ship in ships()
    ])


@pytest.fixture
def docker():
    import docker
    return docker.Client()


@vcr.use_cassette('localstart.yaml')
def test_localstart(capsys, shipment):
    localstart(shipment)
    _, _ = capsys.readouterr()
    localstatus(shipment)
    out, _ = capsys.readouterr()
    assert re.match(r'localship[ \t]+testcont[ \t]+{color}[a-f0-9]{{7}}[ \t]+Up Less than a second'.format(
        color=re.escape(Fore.GREEN)), out.split('\n')[-2])


@vcr.use_cassette('dump.yaml')
def test_dump(capsys, shipment):
    dump(shipment)
    dump1, _ = capsys.readouterr()
    assert dump1 != ''
    with tempfile.NamedTemporaryFile(mode='w') as tmp:
        tmp.write(dump1)
        tmp.flush()
        dump(load_from_yaml(tmp.name))
    dump2, _ = capsys.readouterr()
    assert dump1 == dump2


@vcr.use_cassette('makedeb.yaml')
def test_makedeb(shipment, tmpdir):
    makedeb(shipment, packagename='test-package', distribution='trusty', urgency='high', target=tmpdir.dirname)
    assert tmpdir.ensure_dir('debian')
