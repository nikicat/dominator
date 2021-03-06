FROM yandex/trusty

RUN DEBIAN_FRONTEND=noninteractive apt-get install -yyq python3-pip strace git mercurial
ADD . /root/dominator
RUN pip3 install file:///root/dominator#egg=dominator[full]

ADD dominator/actions/settings.docker.yaml /etc/dominator/settings.yaml

VOLUME /var/lib/dominator
VOLUME /run/docker.sock

CMD dominator -l debug -c - run
