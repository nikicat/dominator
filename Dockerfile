FROM yandex/ubuntu:14.04

RUN DEBIAN_FRONTEND=noninteractive apt-get install -yyq python3-pip strace
ADD . /tmp/dominator
RUN pip3 install /tmp/dominator
RUN rm -rf /tmp/dominator
RUN pip3 install git+https://github.com/dotcloud/docker-py.git --upgrade

ADD settings.docker.yaml /root/

VOLUME /var/lib/dominator
VOLUME /run/docker.sock

CMD dominator -l debug -s /root/settings.docker.yaml -c - run
