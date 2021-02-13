# mp-plex-proxy
Hack to allow Plex Media Server control my Hauppauge HD-PVR via MediaPortal and ArgusTV



docker build --pull --rm -f "Dockerfile" -t mpplexproxy:latest "."
docker run -p 80:80  -it mpplexproxy:latest


