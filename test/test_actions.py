import tempfile
import logging
import re
import datetime

import pytest
from vcr import VCR
from colorama import Fore

from dominator.entities import LocalShip, Container, Image, Shipment
from dominator import actions
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


@pytest.fixture
def shipment():
    shipment = Shipment(
        name='test-shipment',
        containers=[
            Container(
                name='testcont',
                ship=ship,
                image=Image('busybox'),
                command='sleep 10')
            for ship in ships()
        ],
    )
    shipment.version = '1.2.3-alpha-123abcdef'
    shipment.author = 'John Doe'
    shipment.author_email = 'nobody@nonexistent.com'
    shipment.home_page = 'https://nonexistent.com/~nobody'
    shipment.timestamp = datetime.datetime(2000, 1, 1)
    return shipment


@pytest.fixture
def docker():
    import docker
    return docker.Client()


@vcr.use_cassette('localstart.yaml')
def test_localstart(capsys, shipment):
    actions.localstart(shipment)
    _, err = capsys.readouterr()
    assert err == ''

    actions.localstatus(shipment)
    out, _ = capsys.readouterr()
    assert re.match(r'test-shipment[ \t]+localship[ \t]+testcont[ \t]+{color}[a-f0-9]{{7}}[ \t]+Up Less than a second'
                    .format(color=re.escape(Fore.GREEN)), out.split('\n')[-2])

    actions.localrestart(shipment)
    _, err = capsys.readouterr()
    assert err == ''

    actions.stop(shipment)
    _, err = capsys.readouterr()
    assert err == ''


@vcr.use_cassette('dump.yaml')
def test_dump(capsys, shipment):
    actions.dump(shipment)
    dump1, _ = capsys.readouterr()
    assert dump1 != ''
    with tempfile.NamedTemporaryFile(mode='w') as tmp:
        tmp.write(dump1)
        tmp.flush()
        actions.dump(actions.load_from_yaml(tmp.name))
    dump2, _ = capsys.readouterr()
    assert dump1 == dump2


@vcr.use_cassette('makedeb.yaml')
def test_makedeb(shipment, tmpdir):
    actions.makedeb(shipment, packagename='test-package', distribution='trusty', urgency='high', target=tmpdir.dirname)
    assert tmpdir.ensure_dir('debian')
