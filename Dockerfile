FROM yandex/ubuntu:14.04

RUN DEBIAN_FRONTEND=noninteractive apt-get install -yyq python3-pip strace git mercurial
ADD . /root/dominator
RUN pip3 install --process-dependency-links -e /root/dominator

ADD settings.docker.yaml /etc/dominator/settings.yaml

VOLUME /var/lib/dominator
VOLUME /run/docker.sock

CMD dominator -l debug -c - run
