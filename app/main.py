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
from logging.config import dictConfig

# URL format: <protocol>://<username>:<password>@<hostname>:<port>, example: https://test:1234@localhost:9981
config = {
    'bindAddr': '',
    'argustvURL': os.environ.get('ARGUSTV_URL') or 'http://192.168.1.4:49943',
    'argustvProxyURL': os.environ.get('PROXY_URL') or 'http://192.168.1.4:8081',
    'rtspHost' : os.environ.get('RTSP_HOST') or '192.168.1.4',
    'tunerCount': os.environ.get('TVH_TUNER_COUNT') or 1,  # number of tuners in tvh
    'tvhWeight': os.environ.get('TVH_WEIGHT') or 300,  # subscription priority
    'chunkSize': os.environ.get('TVH_CHUNK_SIZE') or 1024*1024,  # usually you don't need to edit this
    'streamProfile': os.environ.get('TVH_PROFILE') or 'pass',  # specifiy a stream profile that you want to use for adhoc transcoding in tvh, e.g. mp4
    'loglevel': os.environ.get('LOGLEVEL') or 'INFO'
}

dictConfig({
    'version': 1,
    'formatters': {'default': {
        'format': '[%(asctime)s] %(levelname)s in %(module)s: %(message)s',
    }},
    'handlers': {'wsgi': {
        'class': 'logging.StreamHandler',
        'stream': 'ext://flask.logging.wsgi_errors_stream',
        'formatter': 'default'
    }},
    'root': {
        'level': config['loglevel'],
        'handlers': ['wsgi']
    }
})

app = Flask(__name__)


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
  
@app.route('/record', methods=['GET'])
def record():
    app.logger.debug("RECORD request endpoint:\n%s", request.endpoint)
    app.logger.debug("RECORD request method: \n%s", request.method)
    app.logger.debug("RECORD request headers: \n%s", request.headers)
    app.logger.debug("RECORD request args: \n%s", request.args)
    app.logger.debug("RECORD request remote_addr: \n%s", request.remote_addr)
    channelId = request.args.get('channel')
    recordResp = None
    try:
        requestedChannel = _getChannel(channelId)
        existingLiveStream = _getLiveStream()
        # fill in the record data
        recordData = {
            'Channel': requestedChannel,
        }
        app.logger.info("------------------------RECORD channel: %s", requestedChannel['DisplayName'])
        
        if existingLiveStream != None:
            app.logger.info("------------------------STREAM EXISTS: %s", existingLiveStream['Channel']['DisplayName'])
            # wait for keep alive thread to be set to False - means existing FFMPEG process is done
            _stopLiveStream()
            waitForThread = True
            global keepThreadGoing
            while waitForThread:
                app.logger.info("Waiting for existing ffmpeg process to finish and RTSP stream to close...")
                time.sleep(1)
                stream = _getLiveStream()                
                if keepThreadGoing == False and stream == None:
                    # thread is done and stream is closed
                    waitForThread = False
        
        # start the recording
        tunelivestreamUrl = '%s/ArgusTV/Control/TuneLiveStream' % (config['argustvURL'])
        headers = {'Content-type': 'application/json'}
        recordResp = requests.post(tunelivestreamUrl, json=recordData, headers=headers)
        app.logger.debug("Record response: %s", recordResp.text)

        j = recordResp.json()
        if j['LiveStreamResult'] == 0:
            vidUrl = j['LiveStream']['RtspUrl']
            app.logger.info("RTSP URL: %s", vidUrl)

            app.logger.info("Creating new ffmpeg process")
            process = (
                ffmpeg
                .input(vidUrl, format="rtsp")
                .output('-', format="mpegts", vcodec="copy", acodec="copy", bufsize=config['chunkSize'])
                .run_async(pipe_stdout=True)
            )
              
            return Response(
                _keepReadingFromFfmpeg(process),
                headers={   
                    'Cache-Control': 'no-cache',
                    'Pragma': 'no-cache',
                    'Content-Type': 'video/mp4'
                },

                )
        else:
            app.logger.error("Could not record: %s", "Unknown error" if recordResp == None else recordResp.text)
            return Response("{'error':'could not setup tuner'}", status=500, mimetype='application/json')

    except Exception as e:
        app.logger.error("Could not start recording: %s", repr(e))
        return Response("{'error':'" + repr(e) + "'}", status=500, mimetype='application/json')

    return ''

def _getLiveStream():
    app.logger.debug("Get live stream")
    getStreamsUrl = '%s/ArgusTV/Control/GetLiveStreams' % (config['argustvURL'])
    liveStreams = requests.get(getStreamsUrl)
    
    streamsArray = liveStreams.json()
    
    # assuming just 1 live stream
    if len(streamsArray) == 1:
        return streamsArray[0]
    else:
        return None
      
def _stopLiveStream():
    app.logger.debug("Stopping live stream")
    stopStreamUrl = '%s/ArgusTV/Control/StopLiveStream' % (config['argustvURL'])
    headers = {'Content-type': 'application/json'}
    stopResp = requests.post(stopStreamUrl, json=_getLiveStream(), headers=headers)
    app.logger.debug("StopLiveStream response: %s", stopResp.status_code)
    
def _callKeepStreamAlive():
    app.logger.debug("Calling keep stream alive")
    keepStreamAliveUrl = '%s/ArgusTV/Control/KeepStreamAlive' % (config['argustvURL'])
    headers = {'Content-type': 'application/json'}
    stream = _getLiveStream()
    app.logger.debug("---------------keep alive request: %s", str(stream))
    aliveResp = requests.post(keepStreamAliveUrl, json=stream, headers=headers)
    app.logger.debug("---------------keep alive response: %s", aliveResp.text)
    return aliveResp.json()
    
def _keepStreamAliveThread():
    while True:
        global keepThreadGoing
        
        if keepThreadGoing == False:
            app.logger.info("Killing keepAliveThread")
            break
        # live stream will auto close after 1 minute or so - keep alive every 30 seconds
        time.sleep(30)
        app.logger.debug("Thread function: keep stream alive")
        resp = _callKeepStreamAlive()
        if resp['IsAlive'] == False:
            keepThreadGoing = False
            app.logger.info("keepAliveThread found stream was off; exiting")
            break
    

def _keepReadingFromFfmpeg(process):
    app.logger.debug("keepReadingFromFfmpeg")
    stream = _getLiveStream()
    if stream == None:
        app.logger.error("Stream was not found. Exiting")
        process.stdout.close()
        process.wait()
        return
    global keepThreadGoing 
    keepThreadGoing = True
    thread = threading.Thread(target=_keepStreamAliveThread)          
    try:
        # start thread to keep stream alive in Argus
        thread.start()        
        while process.poll() is None:
            packet = process.stdout.read(config['chunkSize'])
            yield packet
    finally:
        app.logger.info("Stop stream")
        _stopLiveStream()
        app.logger.info("End ffmpeg process")
        process.stdout.close()
        process.wait()
        app.logger.info("End keepalive thread")
        keepThreadGoing = False
        thread.join()
    
def _getChannel(channelId):
    getchannelURL = '%s/ArgusTV/Scheduler/ChannelById/%s' %  (config['argustvURL'], channelId)
    channelResp = requests.get(getchannelURL)
    app.logger.debug("Channel response: %s", str(channelResp.json()))
    return channelResp.json()

def _get_channels():
    url = '%s/ArgusTV/Scheduler/Channels/0' % config['argustvURL']

    try:
        r = requests.get(url)
        return r.json()

    except Exception as e:
        app.logger.error('An error occured: %s', repr(e))

if __name__ == '__main__':
    app.run(host ="0.0.0.0", port=5004, threaded=True)
