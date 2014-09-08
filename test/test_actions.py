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
        name='testshipment',
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
    runner = CliRunner()
    result = runner.invoke(actions.containers, ['start'], obj=shipment)
    assert result.exit_code == 0

    result = runner.invoke(actions.containers, ['status', '-d'], obj=shipment)
    assert result.exit_code == 0
    lines = result.output.split('\n')
    assert len(lines) == 2
    print(lines[-2])
    assert re.match(r'testshipment[ \t]+localship[ \t]+testcont[ \t]+[a-f0-9]{7}[ \t]+Up Less than a second[ \t]+',
                    lines[-2])

    result = runner.invoke(actions.containers, ['restart'], obj=shipment)
    assert result.exit_code == 0

    shipment.containers[0].volumes['testconf'].files['testfile'].content = 'some other content'
    result = runner.invoke(actions.containers, ['status', '-d'], obj=shipment)
    assert result.exit_code == 0
    lines = result.output.split('\n')
    assert len(lines) == 6
    assert '++++++' in lines[-3]

    result = runner.invoke(actions.containers, ['start'], obj=shipment)
    assert result.exit_code == 0
    result = runner.invoke(actions.containers, ['status', '-d'], obj=shipment)
    assert result.exit_code == 0
    lines = result.output.split('\n')
    assert len(lines) == 2

    result = runner.invoke(actions.containers, ['stop'], obj=shipment)
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
