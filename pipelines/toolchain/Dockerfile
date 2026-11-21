FROM debian:12.11-slim@sha256:cea2634840f5a87503d8210e4df97b9f23a2acd67ff860a76c133d963032f866

ENV LC_ALL=C.UTF-8 \
    TZ=UTC

RUN echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99snapshot && \
    printf 'Types: deb\nURIs: http://snapshot.debian.org/archive/debian/20250814T000000Z\nSuites: bookworm bookworm-updates\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n\nTypes: deb\nURIs: http://snapshot.debian.org/archive/debian-security/20250814T000000Z\nSuites: bookworm-security\nComponents: main\nSigned-By: /usr/share/keyrings/debian-archive-keyring.gpg\n' > /etc/apt/sources.list.d/debian.sources && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
        make \
        curl \
        ca-certificates \
        unzip \
        xz-utils \
        zlib1g-dev \
        libssl-dev \
        libffi-dev \
        pkg-config && \
    rm -rf /var/lib/apt/lists/*

# Build SQLite 3.49.1
RUN curl -sSL https://sqlite.org/2025/sqlite-amalgamation-3490100.zip -o /tmp/sqlite.zip && \
    echo "6cebd1d8403fc58c30e93939b246f3e6e58d0765a5cd50546f16c00fd805d2c3  /tmp/sqlite.zip" | sha256sum -c - && \
    unzip -q /tmp/sqlite.zip -d /tmp && \
    cd /tmp/sqlite-amalgamation-3490100 && \
    gcc -O2 -fPIC -DSQLITE_ENABLE_FTS5 -DSQLITE_ENABLE_MATH_FUNCTIONS -DSQLITE_THREADSAFE=1 -c sqlite3.c -o sqlite3.o && \
    gcc -shared -o libsqlite3.so sqlite3.o -lm -lpthread -ldl && \
    ar rcs libsqlite3.a sqlite3.o && \
    install -d /usr/local/include /usr/local/lib /usr/local/lib/pkgconfig && \
    install -m 0644 sqlite3.h sqlite3ext.h /usr/local/include/ && \
    install -m 0755 libsqlite3.so /usr/local/lib/libsqlite3.so && \
    install -m 0644 libsqlite3.a /usr/local/lib/libsqlite3.a && \
    printf 'prefix=/usr/local\nexec_prefix=${prefix}\nlibdir=${exec_prefix}/lib\nincludedir=${prefix}/include\n\nName: SQLite\nDescription: SQL database engine\nVersion: 3.49.1\nLibs: -L${libdir} -lsqlite3\nLibs.private: -lm -lpthread -ldl\nCflags: -I${includedir}\n' > /usr/local/lib/pkgconfig/sqlite3.pc && \
    ldconfig /usr/local/lib && \
    rm -rf /tmp/sqlite*

# Build CPython 3.13.3
RUN curl -sSL https://www.python.org/ftp/python/3.13.3/Python-3.13.3.tar.xz -o /tmp/python.tar.xz && \
    echo "40f868bcbdeb8149a3149580bb9bfd407b3321cd48f0be631af955ac92c0e041  /tmp/python.tar.xz" | sha256sum -c - && \
    tar -xf /tmp/python.tar.xz -C /tmp && \
    cd /tmp/Python-3.13.3 && \
    PKG_CONFIG_PATH=/usr/local/lib/pkgconfig ./configure --prefix=/usr/local && \
    make -j$(nproc) && \
    make install && \
    ldconfig && \
    ln -s /usr/local/bin/python3 /usr/local/bin/python && \
    rm -rf /tmp/Python-3.13.3 /tmp/python.tar.xz

# Build FFmpeg and ffprobe 7.1.1
RUN curl -sSL https://ffmpeg.org/releases/ffmpeg-7.1.1.tar.xz -o /tmp/ffmpeg.tar.xz && \
    echo "733984395e0dbbe5c046abda2dc49a5544e7e0e1e2366bba849222ae9e3a03b1  /tmp/ffmpeg.tar.xz" | sha256sum -c - && \
    tar -xf /tmp/ffmpeg.tar.xz -C /tmp && \
    cd /tmp/ffmpeg-7.1.1 && \
    ./configure --prefix=/usr/local --disable-autodetect --disable-x86asm --disable-doc --disable-ffplay --disable-network && \
    make -j$(nproc) && \
    make install && \
    rm -rf /tmp/ffmpeg*

# Install test wheels
COPY requirements-test.txt /tmp/requirements-test.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r /tmp/requirements-test.txt && \
    rm /tmp/requirements-test.txt

WORKDIR /repo
