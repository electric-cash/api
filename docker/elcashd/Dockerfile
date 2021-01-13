FROM ubuntu:18.04

RUN apt-get update && \
    apt-get -y install --no-install-recommends --no-install-suggests \
    build-essential \
    curl \
    libboost-all-dev \
    libevent-dev \
    libssl-dev \
    libzmq3-dev \
    pkg-config \
    git \
    unzip \
    autoconf \
    ca-certificates
RUN apt-get -y install automake libtool

WORKDIR /source
ARG DEPLOY_USER
ARG DEPLOY_TOKEN
RUN git clone https://$DEPLOY_USER:$DEPLOY_TOKEN@github.com/electric-cash/electric-cash.git
WORKDIR /source/electric-cash
RUN env CFLAGS=-O2 CXXFLAGS=-O2 \
    ./autogen.sh
RUN ./configure --disable-bench --disable-tests --disable-wallet --with-gui=no
RUN make -j`nproc` && make install

RUN mkdir -p /elcash

# logrotate
COPY logrotate-elcashd /etc/logrotate.d/elcashd
COPY elcash.conf /elcash

CMD ["elcashd","-conf=/elcash/elcash.conf"]
