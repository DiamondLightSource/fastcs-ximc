[![CI](https://github.com/DiamondLightSource/fastcs-ximc/actions/workflows/ci.yml/badge.svg)](https://github.com/DiamondLightSource/fastcs-ximc/actions/workflows/ci.yml)
[![Coverage](https://codecov.io/gh/DiamondLightSource/fastcs-ximc/branch/main/graph/badge.svg)](https://codecov.io/gh/DiamondLightSource/fastcs-ximc)
[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)

# fastcs_ximc

FastCS support for XIMC motion controllers

A [FastCS](https://github.com/DiamondLightSource/FastCS) driver for motion
controllers driven by the [libximc](https://libximc.xisupport.com/) library,
such as the Standa 8SMC5. One `XimcController` drives one device, addressed by
a libximc URI, so real controllers (`xi-com://`, `xi-net://`, `xi-udp://`) and
libximc's virtual device (`xi-emu://`) are driven identically.

What            | Where
:---:           | :---:
Source          | <https://github.com/DiamondLightSource/fastcs-ximc>
Docker          | `docker run ghcr.io/diamondlightsource/fastcs-ximc:latest`
Releases        | <https://github.com/DiamondLightSource/fastcs-ximc/releases>

Describe each controller in a `fastcs.yaml`:

```yaml
controllers:
  - id: AXIS1
    type: fastcs_ximc.XimcController
    connections:
      ximc:
        type: fastcs_ximc.XimcConnection
        settings:
          uri: xi-com:///dev/ttyACM0
transport:
  - epicsca: {}
```

then serve it:

```
python -m fastcs_ximc run fastcs.yaml
```

To try it without hardware, point `uri` at a virtual device - libximc creates
the backing file on first use:

```yaml
          uri: xi-emu:///tmp/fastcs-ximc/sim/axis.bin
```

See [docs/index.md](docs/index.md) for the configuration and attribute
reference.
