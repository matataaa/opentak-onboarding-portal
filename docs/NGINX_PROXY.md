# Nginx Config 


the file "nginx.proxy.conf" in the resources folder can be used to proxy traffic to your docker host. 

It passes the original `Host`, `X-Forwarded-For`, `X-Forwarded-Host` and `X-Forwarded-Proto` to the portal. These headers are not a substitute for `FRONTEND_URL`: set it in `.env` to the portal's public URL. At its localhost default, links in emails and event QR codes point at localhost and won't work for recipients (see [DOCKER.md](DOCKER.md)).

If more info is needed it will be added here later.