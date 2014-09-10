Overview
========

[![Build Status](https://travis-ci.org/yandex-sysmon/dominator.svg)](https://travis-ci.org/yandex-sysmon/dominator)
[![Coverage Status](https://img.shields.io/coveralls/yandex-sysmon/dominator.svg)](https://coveralls.io/r/yandex-sysmon/dominator)
[![Latest Version](https://pypip.in/v/dominator/badge.png)](https://pypi.python.org/pypi/dominator/)
[![Dependency Status](https://gemnasium.com/yandex-sysmon/dominator.svg)](https://gemnasium.com/yandex-sysmon/dominator)
[![Gitter Chat](https://badges.gitter.im/yandex-sysmon/dominator.png)](https://gitter.im/yandex-sysmon/dominator)

Dominator is a tool designed for orchestrating Docker containers and ships.
It works in two steps:
 - generate YAML representation of shipment (ships, containers, volumes, files, etc.) from python code (usually packaged and named "obedient")
 - build, deploy, start/stop, etc. containers described by this YAML representation

Why not Maestro-NG, Decking, Centurion, Saltstack, Ansible etc.?
================================================================

Other orchestration tools over Docker could be handy in many cases, and, mostly, are more simple it staighforward to use than Dominator.
But, Dominator in turn has some very unique features:
  - as Obedient is a plain Python code, author creativity is not bounded by means of any static description format (YAML or JSON). You could relativily easy (there are handy helpers) generate description for service of any complexity without any code or symbol duplication.
  - Dominator interacts not only with Docker, but with ships too (using ssh). Main Dominator design rule - it should be single tool to do anything you may want with your cluster - deploy, obtain current state, view logs, gather metrics and performance counters, check high-level availability, test changes.
  - Versioning and easy upgrade/downgrade. As a whole cluster is fully described by single YAML file, it's very easy to compare one revision with another (or same Obedient revision but launched in different environments) to see what is changed. Because of it, downgrade is the same operation as upgrade - in any case Dominator ensures that after deploying all and only all described containers will be running.

Installation
============


Install using pip
-----------------

`pip3 install --user dominator`

Do not forget to update $PATH to launch dominator without full path:

`export PATH=~/.local/bin:$PATH`

Install using apt for Ubuntu >= Trusty (14.04)
----------------------------------------------

```
sudo apt-add-repository -y ppa:yandex-sysmon/dominator
sudo apt-get update
sudo apt-get install -y dominator
```

Bash completion
---------------

Put this to `~/.bashrc` to enable bash completion:

`eval "$(_DOMINATOR_COMPLETE=source dominator)"`

Settings
--------

Generate settings file:

`dominator settings generate`

This command will create `~/.config/dominator/settings.yaml` with default settings and comments. User could override any setting there.

Usage
=====

Of course, you will need a Docker service. Fortunately, it could be placed not only locally, but on the remote server too.

To use remote Docker, change `dockerurl` field in `~/.config/dominator/settings.yaml`.

To start using Dominator, you need at least one Obedient to generate configuration.
Install, for example, ELK (Elasticsearch+Logstash+Kibana) Obedient:

`pip3 install obedient.elk`

Then, follow these steps to launch it on the (local) Docker:

```
$ # List available obedients
$ dominator shipment generate
Usage: dominator shipment generate [OPTIONS] <distribution> <entrypoint>

Error: Missing argument "distribution".  Choose from obedient.elk, obedient.zookeeper, obedient.elasticsearch.
$ # List entrypoints available in obedient.elk
$ dominator shipment generate obedient.elk 
local
$ # Generate YAML for Obedient
$ dominator shipment generate obedient.elk local > elk.local.yaml
$ # Build images using generated YAML
$ dominator -c elk.local.yaml images build
$ # Start and show status of started containers
$ dominator -c elk.local.yaml containers start status
local                localship  zookeeper            f9f9653    Up 2 seconds                  
local                localship  elasticsearch        cb72d48    Up 1 seconds                  
local                localship  kibana               d6384e8    Up Less than a second         
local                localship  nginx                2159a7e    Up Less than a second         
$ # Wait some time till Elasticsearch starts
$ # You could look through it logs while waiting (it will launch less utility):
$ dominator  -c elk.local.yaml containers -c elasticsearch logs
$ curl localhost:9200
{
  "status" : 200,
  "name" : "elasticsearch-localship",
  "version" : {
    "number" : "1.2.1",
    "build_hash" : "6c95b759f9e7ef0f8e17f77d850da43ce8a4b364",
    "build_timestamp" : "2014-06-03T15:02:52Z",
    "build_snapshot" : false,
    "lucene_version" : "4.8"
  },
  "tagline" : "You Know, for Search"
} 
```

Congratulations! You have a working Dominator, take a coffee break!
Now you are ready to start creating your own Obedients.

Obedient creation
-----------------

Create new obedient using skeleton:
`dominator obedient create superduper`
It will create new obedient in ``pwd``/obedient.superduper directory.
```
$ cd obedient.superduper
$ # Edit it if you wish (comments are inside)
$ vim obedient/superduper/__init__.py
$ # When you are ready, install obedient locally in editable mode
$ pip3 install --user -e .
$ # Now your Superduper Obedient is ready to serve! Try it:
$ domiantor shipment generate obedient.superduper local | domiantor -c - containers start status
superduper           localship  superduper           2159a7c    Up Less than a second
```

Usefull options and tips
----------------------

To show image building, pushing or pulling progress insert `-ldebug` option after `dominator`.

If you  deploy containers on remote (non local) ships, then it could be useful to push images to registry right after the build using `dominator -c obedient.yaml images build --push`. Anyway, Dominator will try to push images when starting containers if remote Docker could not find images in the registry.

To speed up push/pull operations you could point `docker-registry` field in `~/.config/domiantor/settings.yaml` to your own registry.

Terminology
===========

 - Container: object describing how to create, start and use Docker container.
 - Image: object describing image needed to start a Container.
 - Service: just a (web) service. E.g. MySQL, Mongo, Elasticsearch, Zookeeper, Travis, etc.
 - Shipment: collection of Containers, Ships, Images, Volumes, Files, Tasks etc. that fully represents some Service.
 - Obedient: python distribution/package that exports functions (via entry points) that return Shipment object.
