#!/usr/bin/env python
#
#   Copyright (C) 2017 Sean D'Epagnier
#
# This Program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation; either
# version 3 of the License, or (at your option) any later version.  

#
# A separate process listens on port 10110 for tcp connections
# any nmea data received is relayed to all clients
#
# serial ports are probed for incomming nmea data
# this daemon translates nmea tcp messages to signalk
# serving as a tcp nmea server and signalk client
# allowing the autopilot to work with an unmodified opencpn
#
# signalk->nmea: pitch, roll, and heading messages
# nmea->signalk: autopilot commands


import sys, select, time, socket
import multiprocessing
import serial
from signalk.client import SignalKClient
from signalk.server import SignalKServer
from signalk.values import *
from signalk.pipeserver import NonBlockingPipe
from serialprobe import SerialProbe
from gpsdpoller import GpsdPoller

import fcntl
# these are not defined in python module
TIOCEXCL = 0x540C
TIOCNXCL = 0x540D

# favor lower priority sources
source_priority = {'gpsd' : 1, 'serial' : 2, 'tcp' : 3, 'none' : 4}

# nmea uses a simple xor checksum
def nmea_cksum(msg):
    value = 0
    for c in msg: # skip over the $ at the begining of the sentence
        value ^= ord(c)
    return value & 255

def check_nmea_cksum(line):
    cksplit = line.split('*')
    try:
        return nmea_cksum(cksplit[0][1:]) == int(cksplit[1], 16)
    except:
        return False

def parse_nmea_gps(line):
    if line[:6] != '$GPRMC':
        return False

    data = line[7:len(line)-3].split(',')
    timestamp = float(data[0])
    speed = float(data[6])
    heading = float(data[7])
                
    return 'gps', {'timestamp': timestamp, 'track': heading, 'speed': speed}


'''
   ** MWV - Wind Speed and Angle
   **
   **
   **
   ** $--MWV,x.x,a,x.x,a*hh<CR><LF>**
   ** Field Number:
   **  1) Wind Angle, 0 to 360 degrees
   **  2) Reference, R = Relative, T = True
   **  3) Wind Speed
   **  4) Wind Speed Units, K/M/N
   **  5) Status, A = Data Valid
   **  6) Checksum
'''
def parse_nmea_wind(line):
    if line[3:6] != 'MWV':
        return False

    data = line.split(',')
    speed = float(data[3])
    speedunit = data[4]
    if speedunit == 'K': # km/h
        speed *= .53995
    elif speedunit == 'M': # m/s
        speed *= 1.94384
    return 'wind', {'direction': float(data[1]), 'speed': speed}

# because serial.readline() is very slow
class LineBufferedSerialDevice(object):
    def __init__(self, path):
        self.device = serial.Serial(*path)
        self.device.timeout=0 #nonblocking
        fcntl.ioctl(self.device.fileno(), TIOCEXCL)

        self.in_buffer = ''
        self.in_lines = []

    def close(self):
        self.device.close()

    def readline(self):
        return self.in_lines.pop() if self.in_lines else ''

    def recv(self):
        data = self.device.read(1024)
        if data:
            lines = (self.in_buffer + data).split('\n')
            self.in_buffer = lines.pop()
            self.in_lines += lines

from signalk.linebuffer import linebuffer
class NMEASerialDevice(object):
    def __init__(self, path):
        self.device = serial.Serial(*path)
        self.device.timeout=0 #nonblocking
        fcntl.ioctl(self.device.fileno(), TIOCEXCL)
        self.b = linebuffer.LineBuffer(self.device.fileno())

#    def recv(self):
#        return self.b.recv()
        
    def readline(self):
        return self.b.readline_nmea()

    def close(self):
        self.device.close()

class NMEASocket(object):
    def __init__(self, connection):
        connection.setblocking(0)
        #super(NMEASocket, self).__init__(connection.fileno())
        self.socket = connection
        self.b = linebuffer.LineBuffer(connection.fileno())
        self.out_buffer = ''
        self.pollout = select.poll()
        self.pollout.register(connection, select.POLLOUT)

    def recv(self):
        return self.b.recv()

    def readline(self):
        return self.b.line_nmea()

    def close(self):
        self.socket.close()

    def send(self, data):
        self.out_buffer += data
        if len(self.out_buffer) > 4096:
            print 'nmea socket out_buffer overflow'
            self.out_buffer = data

    def flush(self):
        if not len(self.out_buffer):
            return
        try:
            if not self.pollout.poll(0):
                print 'nmea socket failed to send'
                return
            count = self.socket.send(self.out_buffer)
            self.out_buffer = self.out_buffer[count:]
        except:
            self.socket.close()

    
class Nmea(object):
    def __init__(self, server, serialprobe):
        self.server = server
        self.values = {'gps': {}, 'wind': {}}

        timestamp = server.TimeStamp('gps')
        
        self.values['gps']['track'] = server.Register(SensorValue('gps/track', timestamp, directional=True))
        self.values['gps']['speed'] = server.Register(SensorValue('gps/speed', timestamp))
        self.values['gps']['source'] = server.Register(StringValue('gps/source', 'none'))

        timestamp = server.TimeStamp('wind')
        self.values['wind']['direction'] = server.Register(SensorValue('wind/direction', timestamp, directional=True))
        self.values['wind']['speed'] = server.Register(SensorValue('wind/speed', timestamp))
        self.values['wind']['source'] = server.Register(StringValue('wind/source', 'none'))

        self.serialprobe = serialprobe
        self.devices = []
        self.devices_lastmsg = {}
        self.probedevice = None
        self.primarydevices = {'gps': None, 'wind': None}
        self.gpsdpoller = GpsdPoller(self)

        self.process = NmeaBridgeProcess()
        self.process.start()
        READ_ONLY = select.POLLIN | select.POLLHUP | select.POLLERR
        self.poller = select.poll()
        self.process_fd = self.process.pipe.fileno()
        self.poller.register(self.process_fd, READ_ONLY)
        self.gps_fd = self.gpsdpoller.process.pipe.fileno()
        self.poller.register(self.gps_fd, READ_ONLY)
        self.device_fd = {}

        self.nmea_times = {}
        self.last_imu_time = time.time()
        self.starttime = time.time()

    def __del__(self):
        if self.gps.process:
            print 'terminate gps process'
            self.gps.process.terminate()
        print 'terminate nmea process'
        self.process.terminate()

    def read_process_pipe(self):
        msgs = self.process.pipe.recv()
        if msgs == 'sockets':
            self.process.sockets = True
        elif msgs == 'nosockets':
            self.process.sockets = False
        else:
            self.handle_messages(msgs, 'tcp')

    def read_serial_device(self, device, serial_msgs):
        t = time.time()
        line = device.readline()
        if not line:
            return
        if self.process.sockets:
            nmea_name = line[:6]
            # do not output nmea data over tcp faster than 2hz
            # for each message time
            if not nmea_name in self.nmea_times or t-self.nmea_times[nmea_name]>.5:
                self.process.pipe.send(line, False)
                self.nmea_times[nmea_name] = t

        self.devices_lastmsg[device] = t
        parsers = []
        if not self.primarydevices['wind'] or self.primarydevices['wind'] == device:
            parsers.append(parse_nmea_wind)
        if self.values['gps']['source'] != 'gpsd' and \
           (not self.primarydevices['gps'] or self.primarydevices['gps'] == device):
            parsers.append(parse_nmea_gps)
                
        for parser in parsers:
            result = parser(line)
            if result:
                name, msg = result
                if not self.primarydevices[name]:
                    print 'found primary serial device for', name
                    self.primarydevices[name] = device
                serial_msgs[name] = msg
                break

    def poll(self):
        t0 = time.time()
        self.probe_serial()

        t1 = time.time()
        # handle tcp nmea messages
        serial_msgs = {}
        while True:
            events = self.poller.poll(0)
            if not events:
                break
            while events:
                event = events.pop()
                fd, flag = event
                if fd == self.process_fd:
                    if flag != select.POLLIN:
                        print 'nmea got flag for process pipe:', flag
                    else:
                        self.read_process_pipe()
                elif fd == self.gps_fd:
                    if flag != select.POLLIN:
                        print 'nmea got flag for gpsdpoller pipe:', flag
                    else:
                        self.gpsdpoller.read()
                else:
                    self.read_serial_device(self.device_fd[fd], serial_msgs)

        t2 = time.time()
        self.handle_messages(serial_msgs, 'serial')
        t3 = time.time()
                
        for device in self.devices:
            # timeout serial devices
            dt = time.time() - self.devices_lastmsg[device]
            if dt > 1:
                print 'dt', dt
            if dt > 15:
                print 'serial device timed out', dt
                self.devices.remove(device)
                del self.devices_lastmsg[device]
                for name in self.primarydevices:
                    if device == self.primarydevices[name]:
                        self.primarydevices[name] = None
                device.close()
        t4 = time.time()

        if self.process.sockets and time.time() - self.last_imu_time > .5 and \
           'imu/pitch' in self.server.values:
            self.send_nmea('APXDR,A,%.3f,D,PTCH' % self.server.values['imu/pitch'].value)
            self.send_nmea('APXDR,A,%.3f,D,ROLL' % self.server.values['imu/heel'].value)
            self.send_nmea('APHDM,%.3f,M' % self.server.values['imu/heading_lowpass'].value)
            self.last_imu_time = time.time()
        t5 = time.time()
        if t5 - t0 > .1:
            print 'nmea poll times', t1-t0, t2-t1, t3-t2, t4-t3, t5-t4
            
    def probe_serial(self):
        # probe new nmea data devices
        if not self.probedevice:
            self.probedevicepath = self.serialprobe.probe('nmea%d' % len(self.devices), [38400, 4800])
            if self.probedevicepath:
                try:
                    self.probedevice = NMEASerialDevice(self.probedevicepath)
                    self.probetime = time.time()
                except serial.serialutil.SerialException:
                    print 'failed to open', self.probedevicepath, 'for nmea data'
                    pass
        elif time.time() - self.probetime > 5:
            print 'nmea serial probe timeout', self.probedevicepath
            self.probedevice = None # timeout
        else:
            # see if the probe device gets a valid nmea message
            if self.probedevice:
                if self.probedevice.readline():
                    print 'new nmea device', self.probedevicepath
                    self.serialprobe.probe_success('nmea%d' % len(self.devices))
                    self.devices.append(self.probedevice)
                    fd = self.probedevice.device.fileno()
                    self.device_fd[fd] = self.probedevice
                    self.poller.register(fd, select.POLLIN)
                    self.devices_lastmsg[self.probedevice] = time.time()
                    self.probedevice = None

    def send_nmea(self, msg):
        line = '$' + msg + ('*%02X' % nmea_cksum(msg))
        self.process.pipe.send(line, False)

    def handle_messages(self, msgs, source):
        for name in msgs:
            if not name in self.values:
                print 'unknown data parsed!', name
                break
            value = self.values[name]

            if source_priority[value['source'].value] < source_priority[source]:
                continue

            msg = msgs[name]
            timestamp = msg['timestamp'] if 'timestamp' in msg else time.time()-self.starttime
            self.server.TimeStamp(name, timestamp)

            for vname in msg:
                if vname != 'timestamp':
                    value[vname].set(msg[vname])
            value['source'].update(source)

    
READ_ONLY = select.POLLIN | select.POLLHUP | select.POLLERR
class NmeaBridgeProcess(multiprocessing.Process):
    def __init__(self):
        self.pipe, pipe = NonBlockingPipe('nmea pipe', True)
        self.sockets = False
        super(NmeaBridgeProcess, self).__init__(target=self.process, args=(pipe,))

    def setup_watches(self, watch=True):
        watchlist = ['ap/enabled', 'ap/mode', 'ap/heading_command', 'gps/source', 'wind/source']
        for name in watchlist:
            self.client.watch(name, watch)

    def receive_nmea(self, line, msgs):
        for parser in self.parsers:
            result = parser(line)
            if result:
                name, msg = result
                msgs[name] = msg
                break

        # also allow ap commands (should we allow via serial too??)
        if line[3:6] == 'APB' and time.time() - self.last_apb_time > 1:
            self.last_apb_time = time.time()
            data = line[7:len(line)-3].split(',')
            if not self.last_values['ap/enabled']:
                self.client.set('ap/enabled', True)

            if self.last_values['ap/mode'] != 'gps':
                client.set('ap/mode', 'gps')

            if abs(self.last_values['ap/heading_command'] - float(data[7])) > .1:
                client.set('ap/heading_command', float(data[7]))

    def new_socket_connection(self, server):
        connection, address = server.accept()
        max_connections = 10
        if len(self.sockets) == max_connections:
            connection.close()
            print 'nmea server has too many connections'
            return
    
        if not self.sockets:
            self.setup_watches()
            self.pipe.send('sockets')

        sock = NMEASocket(connection)
        self.sockets.append(sock)
        #print 'new connection: ', address
        self.addresses[sock] = address
        fd = sock.socket.fileno()
        self.fd_to_socket[fd] = sock

        self.poller.register(sock.socket, READ_ONLY)

    def socket_lost(self, sock):
        #print 'lost connection: ', self.addresses[sock]
        self.sockets.remove(sock)
        if not self.sockets:
            self.setup_watches(False)
            self.pipe.send('nosockets')

        self.poller.unregister(sock.socket)
        fd = sock.socket.fileno()
        del self.fd_to_socket[fd]
        sock.close()

    def update_parsers(self):
        self.parsers = []
        if source_priority[self.last_values['gps/source']] >= source_priority['tcp']:
            self.parsers.append(parse_nmea_gps)

        if source_priority[self.last_values['wind/source']] >= source_priority['tcp']:
            self.parsers.append(parse_nmea_wind)

    def client_message(self, name, value):
        self.last_values[name] = value
        if name == 'gps/source' or name == 'wind/source':
            self.update_parsers()

    def process(self, pipe):
        import os
        print 'nmea bridge on', os.getpid()
        self.pipe = pipe
        self.sockets = []
        self.parsers = []
        self.last_apb_time = time.time()
        def on_con(client):
            print 'nmea client connected'
            if self.sockets:
                self.setup_watches()

        while True:
            time.sleep(2)
            try:
                self.client = SignalKClient(on_con, 'localhost', autoreconnect=True)
                break
            except:
                pass

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setblocking(0)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        port = 20220
        try:
            server.bind(('0.0.0.0', port))
        except:
            print 'nmea_bridge: bind failed.'
            exit(1)
        print 'listening on port', port, 'for nmea connections'

        server.listen(5)

        self.last_values = {'ap/enabled': False, 'ap/mode': 'N/A', 'ap/heading_command' : 1000, 'gps/source' : 'none', 'wind/source' : 'none'}
        self.addresses = {}
        cnt = 0

        self.poller = select.poll()
        self.poller.register(server, READ_ONLY)
        self.poller.register(pipe, READ_ONLY)
        self.fd_to_socket = {server.fileno() : server, pipe.fileno() : pipe}

        msgs = {}
        while True:
            timeout = 100 if self.sockets else 10000
            t0 = time.time()
            events = self.poller.poll(timeout)
            t1 = time.time()
            while events:
                fd, flag = events.pop()
                sock = self.fd_to_socket[fd]

                if flag & (select.POLLHUP | select.POLLERR):
                    if sock == server:
                        print 'nmea bridge lost server connection'
                        exit(2)
                    if sock == pipe:
                        print 'nmea bridge pipe to autopilot'
                        exit(2)
                    self.socket_lost(sock)
                elif sock == server:
                    self.new_socket_connection(server)
                elif sock == pipe:
                    while True: # receive all messages in pipe
                        msg = self.pipe.recv()
                        if not msg:
                            break
                        msg += '\r\n'
                        for sock in self.sockets:
                            sock.send(msg)
                elif flag & select.POLLIN:
                    if not sock.recv():
                        self.socket_lost(sock)
                    else:
                        while True:
                            line = sock.readline()
                            if not line:
                                break
                            self.receive_nmea(line, msgs)

            t2 = time.time()
            if msgs:
                if self.pipe.send(msgs):
                    msgs = {}

            t3 = time.time()
            try:
                signalk_msgs = self.client.receive()
                for name in signalk_msgs:
                    self.client_message(name, msgs[name]['value'])
            except:
                pass

            t4 = time.time()
            for sock in self.sockets:
                sock.flush()
            t5 = time.time()

            dt = t5 - t0
            if dt < .1:
                time.sleep(.1 - dt)
            else:
                if t5-t1 > .1:
                    print 'nmea process loop too slow:', t1-t0, t2-t1, t3-t2, t4-t3, t5-t4

if __name__ == '__main__':
    if os.system('sudo chrt -pf 1 %d 2>&1 > /dev/null' % os.getpid()):
      print 'warning, failed to make nmea process realtime'
    server = SignalKServer()
    serialprobe = SerialProbe()
    nmea = Nmea(server, serialprobe)

    while True:
        nmea.poll()
        server.HandleRequests(.1)