from gevent import monkey; monkey.patch_all()

import time
import os
import requests
import logging
import json
import ffmpeg
import re
# from gevent.pywsgi import WSGIServer
from flask import Flask, Response, request, jsonify, abort, render_template, redirect

app = Flask(__name__)

# URL format: <protocol>://<username>:<password>@<hostname>:<port>, example: https://test:1234@localhost:9981
config = {
    'bindAddr': '',
    'argustvURL': os.environ.get('ARGUSTV_URL') or 'http://192.168.1.4:49943',
    'argustvProxyURL': os.environ.get('ARGUSTV_PROXY_URL') or 'http://10.49.1.153',
    'rtspHost' : os.environ.get('RTSP_HOST') or '192.168.1.4',
    'tunerCount': os.environ.get('TVH_TUNER_COUNT') or 1,  # number of tuners in tvh
    'tvhWeight': os.environ.get('TVH_WEIGHT') or 300,  # subscription priority
    'chunkSize': os.environ.get('TVH_CHUNK_SIZE') or 1024*1024,  # usually you don't need to edit this
    'streamProfile': os.environ.get('TVH_PROFILE') or 'pass'  # specifiy a stream profile that you want to use for adhoc transcoding in tvh, e.g. mp4
}

discoverData = {
    'FriendlyName': 'mp-plex-proxy',
    'Manufacturer' : 'Silicondust',
    'ModelNumber': 'HDTC-2US',
    'FirmwareName': 'hdhomeruntc_atsc',
    'TunerCount': int(config['tunerCount']),
    'FirmwareVersion': '20150826',
    'DeviceID': '12345678',
    'DeviceAuth': 'test1234',
    'BaseURL': '%s' % config['argustvProxyURL'],
    'LineupURL': '%s/lineup.json' % config['argustvProxyURL']
}

@app.route('/discover.json')
def discover():
    return jsonify(discoverData)


@app.route('/lineup_status.json')
def status():
    return jsonify({
        'ScanInProgress': 0,
        'ScanPossible': 1,
        'Source': "Cable",
        'SourceList': ['Cable']
    })


@app.route('/lineup.json')
def lineup():
    lineup = []

    for c in _get_channels():
        url = '%s/record?channel=%s' % (config['argustvProxyURL'], c['ChannelId'])
        lineup.append({'GuideNumber': str(c['LogicalChannelNumber']),
                        'GuideName': c['DisplayName'],
                        'URL': url
                        })

    return jsonify(lineup)


@app.route('/lineup.post', methods=['GET', 'POST'])
def lineup_post():
    return ''

@app.route('/')
@app.route('/device.xml')
def device():
    return render_template('device.xml',data = discoverData),{'Content-Type': 'application/xml'}

def keepReading(process):
    logging.warning("IN keepReading()")
    try:
        while process.poll() is None:
            packet = process.stdout.read(config['chunkSize'])
            yield packet
    finally:
        process.stdout.close()
        process.wait()

@app.route('/record', methods=['GET'])
def record():
    channelId = request.args.get('channel')

    try:
        # get the channel info
        getchannelURL = '%s/ArgusTV/Scheduler/ChannelById/%s' %  (config['argustvURL'], channelId)
        channel = requests.get(getchannelURL)

        logging.warning("Channel response: " + str(channel.text))
        recordData = {
            'Channel': json.loads(channel.text)
        }
        
        logging.warning("recordData: " + str(recordData))
        # start the recording
        tunelivestreamUrl = '%s/ArgusTV/Control/TuneLiveStream' % (config['argustvURL'])
        headers = {'Content-type': 'application/json'}
        recordResp = requests.post(tunelivestreamUrl, json=recordData, headers=headers)
        logging.warning("Record response: " + recordResp.text)

        # Response format:
        # {
        #     "LiveStream": {
        #         "CardId": "4", 
        #         "Channel": {
        #         "BroadcastStart": null, 
        #         "BroadcastStop": null, 
        #         "ChannelId": "840d61cd-5e47-4464-9518-fbcf474d3fb1", 
        #         "ChannelType": 0, 
        #         "CombinedDisplayName": "30 - 30 TSN5", 
        #         "DefaultPostRecordSeconds": null, 
        #         "DefaultPreRecordSeconds": null, 
        #         "DisplayName": "30 TSN5", 
        #         "GuideChannelId": "b6eee01d-4074-48fc-a543-0cebf5b0feb1", 
        #         "Id": 446, 
        #         "LogicalChannelNumber": 30, 
        #         "Sequence": 1, 
        #         "Version": 24, 
        #         "VisibleInGuide": true
        #         }, 
        #         "RecorderTunerId": "134d41e5-deac-4cf3-b9b6-efd931fbc05b", 
        #         "RtspUrl": "rtsp://homer:554/stream4.0", 
        #         "StreamLastAliveTimeUtc": "/Date(1612735516880)/", 
        #         "StreamStartedTime": "/Date(1612735516880-0500)/", 
        #         "TimeshiftFile": "\\\\HOMER\\homerd\\mptvdtimeshift\\live4-0.tsbuffer"
        #     }, 
        #     "LiveStreamResult": 0
        # }
        j = recordResp.json()
        if j['LiveStreamResult'] == 0:
            time.sleep(3); # let the stream get setup
            vidUrl = j['LiveStream']['RtspUrl']
            # name resolution may not work, so replace with provided ip
            vidUrl = re.sub("rtsp://\w*:554", "rtsp://%s:554" % config['rtspHost'] , vidUrl)
            logging.warning("RTSP URL: " + vidUrl)

            logging.warning("------------------------RECORD--------------------------------")
            process = (
                ffmpeg
                .input(vidUrl, r="29.97", format="rtsp")
                .output('-', r="29.97", format="mpegts", vcodec="copy", acodec="copy", bufsize=config['chunkSize'])
                .run_async(pipe_stdout=True)
            )
            return Response(
                keepReading(process),
                headers={   
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache',
                    'Content-Type': 'video/mp4'
                },

                )
        else:
            logging.error("Could not record")
            return Response("{'error':'could not setup tuner'}", status=500, mimetype='application/json')

    except Exception as e:
        logging.error("Could not start recording:" + repr(e))
        return Response("{'error':'" + repr(e) + "'}", status=500, mimetype='application/json')

    return ''

def _get_channels():
    url = '%s/ArgusTV/Scheduler/Channels/0' % config['argustvURL']

    try:
        r = requests.get(url)
        return r.json()

    except Exception as e:
        print('An error occured: ' + repr(e))


if __name__ == '__main__':
    app.run(host ="0.0.0.0", port=5004, threaded=True)
    # http = WSGIServer((config['bindAddr'], 5004), app.wsgi_app)
    # http.serve_forever()
