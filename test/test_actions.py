import tempfile
import logging
import re
import datetime
import shutil
import os

import pytest
from vcr import VCR
from colorama import Fore

from dominator import entities
from dominator import actions
from dominator.utils import settings as _settings


vcr = VCR(cassette_library_dir='test/fixtures/vcr_cassettes')


@pytest.yield_fixture(autouse=True)
def settings():
    _settings['configvolumedir'] = '/tmp/dominator-test-config'
    # FIXME: use https endpoint because vcrpy doesn't handle UnixHTTPConnection
    _settings['dockerurl'] = 'http://localhost:4243'
    _settings['deploy-image'] = 'nikicat/dominator'

    try:
        os.mkdir(_settings['configvolumedir'])
        yield _settings
    finally:
        shutil.rmtree(_settings['configvolumedir'], ignore_errors=True)


@pytest.fixture(autouse=True)
def logs():
    logging.disable(level=logging.INFO)


@pytest.fixture
def ships():
    return [entities.LocalShip()]


@vcr.use_cassette('shipment.yaml')
@pytest.fixture
def shipment():
    shipment = entities.Shipment(
        name='test-shipment',
        containers=[
            entities.Container(
                name='testcont',
                ship=ship,
                image=entities.Image('busybox'),
                command='sleep 10',
                volumes={
                    'testconf': entities.ConfigVolume(
                        dest='/tmp',
                        files={
                            'testfile': entities.TextFile(text='some content')
                        },
                    ),
                },
            ) for ship in ships()
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
def test_start(capsys, shipment):
    actions.start(shipment)
    _, err = capsys.readouterr()
    assert err == ''

    actions.status(shipment, showdiff=True)
    out, _ = capsys.readouterr()
    lines = out.split('\n')
    assert len(lines) == 2
    assert re.match(r'test-shipment[ \t]+localship[ \t]+testcont[ \t]+{color}[a-f0-9]{{7}}[ \t]+Up Less than a second'
                    .format(color=re.escape(Fore.GREEN)), lines[-2])

    actions.restart(shipment)
    _, _ = capsys.readouterr()

    shipment.containers[0].volumes['testconf'].files['testfile'].content = 'some other content'
    actions.status(shipment, showdiff=True)
    out, _ = capsys.readouterr()
    lines = out.split('\n')
    assert len(lines) == 6
    assert '++++++' in lines[-3]

    actions.start(shipment)
    actions.status(shipment, showdiff=True)
    out, _ = capsys.readouterr()
    lines = out.split('\n')
    assert len(lines) == 2

    actions.stop(shipment)


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
