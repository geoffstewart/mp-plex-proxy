from gevent import monkey; monkey.patch_all()

import time
import os
import requests
import logging
import json
import ffmpeg
import re
import threading
from flask import Flask, Response, request, jsonify, abort, render_template, redirect

app = Flask(__name__)

# URL format: <protocol>://<username>:<password>@<hostname>:<port>, example: https://test:1234@localhost:9981
config = {
    'bindAddr': '',
    'argustvURL': os.environ.get('ARGUSTV_URL') or 'http://192.168.1.4:49943',
    'argustvProxyURL': os.environ.get('PROXY_URL') or 'http://192.168.1.4:8081',
    'rtspHost' : os.environ.get('RTSP_HOST') or '192.168.1.4',
    'tunerCount': os.environ.get('TVH_TUNER_COUNT') or 1,  # number of tuners in tvh
    'tvhWeight': os.environ.get('TVH_WEIGHT') or 300,  # subscription priority
    'chunkSize': os.environ.get('TVH_CHUNK_SIZE') or 1024*1024,  # usually you don't need to edit this
    'streamProfile': os.environ.get('TVH_PROFILE') or 'pass'  # specifiy a stream profile that you want to use for adhoc transcoding in tvh, e.g. mp4
}

# this response is to trick Plex into thinking the tuner is a silicondust card
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

# global flag to control keep alive thread - assuming just one stream at a time
keepThreadGoing = False

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

def getLiveStream():
    logging.info("Get live stream")
    getStreamsUrl = '%s/ArgusTV/Control/GetLiveStreams' % (config['argustvURL'])
    liveStreams = requests.get(getStreamsUrl)
    
    streamsArray = liveStreams.json()
    
    # assuming just 1 live stream
    if len(streamsArray) == 1:
        return streamsArray[0]
    else:
        return None
      
def stopLiveStream():
    logging.info("Stopping live stream")
    stopStreamUrl = '%s/ArgusTV/Control/StopLiveStream' % (config['argustvURL'])
    headers = {'Content-type': 'application/json'}
    stopResp = requests.post(stopStreamUrl, json=getLiveStream(), headers=headers)
    
def keepStreamAlive():
    logging.info("Keep stream alive")
    keepStreamAliveUrl = '%s/ArgusTV/Control/KeepStreamAlive' % (config['argustvURL'])
    headers = {'Content-type': 'application/json'}
    stream=getLiveStream()
    logging.warning("---------------keep alive request: " + str(stream))
    aliveResp = requests.post(keepStreamAliveUrl, json=stream, headers=headers)
    logging.warning("---------------keep alive response: " + aliveResp.text)
    
def keepStreamAliveThread():
    while True:
        global keepThreadGoing
        
        logging.warning("THREAD")
        if keepThreadGoing == False:
            logging.warning("Killing keepAliveThread")
            break
        # live stream will auto close after 1 minute or so - keep alive every 30 seconds
        time.sleep(30)
        logging.warning("Thread function: keep stream alive")
        keepStreamAlive()
    

def keepReadingFromFfmpeg(process):
    logging.info("keepReadingFromFfmpeg")
    stream = getLiveStream()
    if stream == None:
        logging.error("Stream was not found. Exiting")
        process.stdout.close()
        process.wait()
        return
    global keepThreadGoing 
    keepThreadGoing = True
    thread = threading.Thread(target=keepStreamAliveThread)          
    try:
        # start thread to keep stream alive in Argus
        thread.start()        
        while process.poll() is None:
            packet = process.stdout.read(config['chunkSize'])
            yield packet
    finally:
        logging.warning("stop stream")
        stopLiveStream()
        logging.warning("End keepalive thread")
        keepThreadGoing = False
        thread.join()
        logging.warning("End ffmpeg process")
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
        
        logging.info("recordData: " + str(recordData))
        # start the recording
        tunelivestreamUrl = '%s/ArgusTV/Control/TuneLiveStream' % (config['argustvURL'])
        headers = {'Content-type': 'application/json'}
        recordResp = requests.post(tunelivestreamUrl, json=recordData, headers=headers)
        logging.info("Record response: " + recordResp.text)

        j = recordResp.json()
        if j['LiveStreamResult'] == 0:
            time.sleep(3); # let the stream get setup
            vidUrl = j['LiveStream']['RtspUrl']
            # name resolution may not work, so replace with provided ip
            # vidUrl = re.sub("rtsp://\w*:554", "rtsp://%s:554" % config['rtspHost'] , vidUrl)
            logging.warning("RTSP URL: " + vidUrl)

            logging.warning("------------------------RECORD--------------------------------")
            process = (
                ffmpeg
                .input(vidUrl, format="rtsp")
                .output('-', format="mpegts", vcodec="copy", acodec="copy", bufsize=config['chunkSize'])
                .run_async(pipe_stdout=True)
            )
            return Response(
                keepReadingFromFfmpeg(process),
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
    format = "%(asctime)s: %(message)s"
    logging.basicConfig(format=format, level=logging.INFO,
                        datefmt="%H:%M:%S")

    app.run(host ="0.0.0.0", port=5004, threaded=True)
