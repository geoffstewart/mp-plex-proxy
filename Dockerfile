FROM tiangolo/uwsgi-nginx-flask:python3.8

# RUN apk --update add bash vim build-base
ENV STATIC_URL /static
ENV STATIC_PATH /var/www/app/static
RUN /usr/local/bin/python -m pip install --upgrade pip
RUN apt-get update && apt-get install -y ffmpeg
COPY ./requirements.txt /var/www/requirements.txt
RUN pip install -r /var/www/requirements.txt
COPY ./app /app
