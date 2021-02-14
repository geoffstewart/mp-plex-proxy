# mp-plex-proxy
Hack to allow Plex Media Server control my Hauppauge HD-PVR via MediaPortal and ArgusTV. This project was inspired by https://github.com/jkaberg/tvhProxy.  Instead of TV Headend controlling the plex-unsupported devices, it's MediaPortal with ArgusTV in front. Also, I tried to simplify deployment by making a docker container for the whole proxy solution.

You can build and run a docker container on the same machine hosting your MediaPortal install. I developed this on a laptop that was not co-located (and on wifi), and the streams going back and forth from the laptop were choppy. But, it's perfectly smooth when co-located.

To build the container:
```
docker build --pull --rm -f "Dockerfile" -t mpplexproxy:latest "."
```

Modify the environment variables as needed. You should not use 127.0.0.1 since the docker container will have its own ip address that differs from the host; also, adding the host for your windows server will resolve the RTSP URL properly.
```
docker run -dit -p 8081:80 `
    --add-host homer:192.168.1.4 `
    -e ARGUSTV_URL="http://192.168.1.4:49943" `
    -e PROXY_URL="http://192.168.1.4:8081" `
    -e RTSP_HOST="192.168.1.4" `
    -e LOGLEVEL="DEBUG" `
    --name mpplexproxy `
    mpplexproxy:latest
```

