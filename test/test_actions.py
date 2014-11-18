import logging
import re
import datetime
import shutil
import os
import os.path

import yaml
import pytest
from vcr import VCR
from click.testing import CliRunner

from dominator import entities
from dominator import actions
from dominator.utils import settings as _settings


vcr = VCR(cassette_library_dir='test/fixtures/vcr_cassettes')


@pytest.yield_fixture(autouse=True)
def settings():
    _settings['configvolumedir'] = '/tmp/dominator-test-config'
    # FIXME: use https endpoint because vcrpy doesn't handle UnixHTTPConnection
    _settings['docker.url'] = 'http://localhost:4243'
    _settings['docker.namespace'] = 'yandex'

    try:
        os.mkdir(_settings['configvolumedir'])
        yield _settings
    finally:
        shutil.rmtree(_settings['configvolumedir'], ignore_errors=True)


@pytest.fixture(autouse=True)
def logs():
    logging.disable(level=logging.INFO)


@pytest.fixture
def ship():
    return entities.LocalShip()


@vcr.use_cassette('shipment.yaml')
@pytest.fixture
def shipment(ship):
    container = entities.Container(
        name='testcont',
        image=entities.Image('busybox', namespace=None),
        command='sleep 10',
        volumes={
            'testconf': entities.ConfigVolume(
                dest='/tmp',
                files={
                    'testfile': entities.TextFile(text='some content')
                },
            ),
        },
    )
    ship.place(container)
    ship.expose_ports(range(10000, 10010))

    shipment = entities.Shipment(
        name='testshipment',
        ships={ship.name: ship},
    )
    shipment.version = '1.2.3-alpha-123abcdef'
    shipment.author = 'John Doe'
    shipment.author_email = 'nobody@nonexistent.com'
    shipment.home_page = 'https://nonexistent.com/~nobody'
    shipment.timestamp = datetime.datetime(2000, 1, 1)
    shipment.make_backrefs()
    return shipment


@vcr.use_cassette('localstart.yaml')
def test_start(capsys, shipment):
    runner = CliRunner()
    result = runner.invoke(actions.container, ['start'], obj=shipment)
    assert result.exit_code == 0

    result = runner.invoke(actions.container, ['status', '-d'], obj=shipment)
    assert result.exit_code == 0
    lines = result.output.split('\n')
    assert len(lines) == 3
    assert re.match(r'[ \t]*localship:testcont[ \t]+[a-f0-9]{7}[ \t]+Up Less than a second[ \t]*',
                    lines[-2])

    result = runner.invoke(actions.container, ['restart'], obj=shipment)
    assert result.exit_code == 0

    next(shipment.containers).volumes['testconf'].files['testfile'].data = 'some other content'
    result = runner.invoke(actions.container, ['status', '-d'], obj=shipment)
    assert result.exit_code == 2
    lines = result.output.split('\n')
    assert len(lines) == 7
    assert '++++++' in lines[-3]

    result = runner.invoke(actions.container, ['start'], obj=shipment)
    assert result.exit_code == 0
    result = runner.invoke(actions.container, ['status', '-d'], obj=shipment)
    assert result.exit_code == 0
    lines = result.output.split('\n')
    assert len(lines) == 3

    result = runner.invoke(actions.container, ['stop'], obj=shipment)
    assert result.exit_code == 0


@vcr.use_cassette('dump.yaml')
def test_dump(capsys, shipment):
    dump1 = yaml.dump(shipment)
    assert dump1 != 'null'
    dump2 = yaml.dump(yaml.load(dump1))
    assert dump1 == dump2


@vcr.use_cassette('makedeb.yaml')
def test_makedeb(shipment, tmpdir):
    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(actions.shipment, ['makedeb', 'test-package', 'trusty', 'high'], obj=shipment)
        assert result.exit_code == 0
        assert os.path.isdir('debian')
