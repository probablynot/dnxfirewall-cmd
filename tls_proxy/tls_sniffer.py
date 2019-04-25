#!/usr/bin/env python3

from socket import socket, inet_aton, inet_ntoa, AF_PACKET, SOCK_RAW
import struct
import threading
import binascii
import codecs
import time

class Sniffer:
    ''' This class is the initial Sniffer class, it acts as a wrapper for all other classes for the most part. Once
    interesting traffic (Client Hello) is identified it will create a thread and send all relevant information as well
    as the socket to the SSL Handler Class, where the rest of the handshake will be tracked, logged, and parsed. The
    SSL Handler class will not return back to this class as it is in a thread and would not be able to return necesarry
    packet information. The SSL Handler will envoke the return to the TLS Proxy once it completes the process for each
    individual thread '''
    def __init__(self, iface, action):
        self.iface = iface
        self.action = action
        self.s = socket(AF_PACKET, SOCK_RAW)
        self.s.bind((self.iface, 3))

        self.ack_check = 0
        
    ''' Starting General socket to look for client hello message '''
    def Start(self):
        print('[+] Sniffing on interface {}'.format(self.iface))
        while True:
            data, addr = self.s.recvfrom(8000)
            try:
                Header = HeaderParse(data, addr)
                hs_type, tcp_info, sport = Header.Parse()
                ''' Will match any client hello message which indicates an attempted encrpytion connection
                handshake to a remote server.
                Upon matching, will start a thread to handle the rest of the connection to ensure
                persistent communication can be handled within each ssl handler class instance '''
                if (hs_type == 1):
                    SSLHandler = SSLHandlerThread(self.action, self.s, data, tcp_info, sport)
                    threading.Thread(target=SSLHandler.Start).start()
            except AttributeError:
                pass
            except Exception as E:
                pass

class SSLHandlerThread:
    ''' This class is called from the main Sniffer class in the event that a Client Hello is detected.
    This class will start tracking the SSL/TLS handshake based on the information given to it from the 
    Main Sniffer regarding the Client Hello. This class will have access to the socket of the Sniffer
    directly and will call the Header Parse class to determine whether it is apart of the initial handshake.
    This will keep each handshake in its own thread for persistent tracking until it completes. After the
    SSL Server Hello packet is combined up until the Server Hello end it will send the reorder and reformat
    the packet to look as though it was sent as one, then will send the packet to the SSL Parse class to have
    the SSL Cert in the chain split from the packet and sent to the TLS Proxy. '''
    def __init__(self, action, socket, data, tcp_info, client_port):
        seq_number, ack_number, _, tcp_segment_length = tcp_info
        self.action = action
        self.s = socket
        self.data = data
#        self.ack_check = ack_check
        self.client_port = client_port
        self.active = True

        self.tcp_header_length = 0

        self.ack_offset = ack_number
        self.expected_ack_number = seq_number + tcp_segment_length
        
        self.sequence_offset = 0
        self.initial_sequence_number = ack_number
        self.expected_sequence_number = ack_number
        self.valid_sequence = set()

        self.ssl_packet = {}

        self.handshake = {'server_hello': {
                            'status': False,
                            'sequence': 0,
                            'segment': 0}, 
                        'hello_done': {
                            'status': False,
                            'sequence': 0,
                            'segment': 0
                        }}

        threading.Thread(target=self.Timer).start()

    ''' SSL Handler Thread Logic contained, ensures packets are part of same SSL Handshake,
    Adds packets to a dictionary, then will send packet for reorder, header removal, and joining to
    then be sent to Certificate Parser prior to being sent back to the proxy '''
    def Start(self):
        server_hello = self.handshake['server_hello']
        hello_done = self.handshake['hello_done']
        start = time.time()
        print('+'*30)
        print('CLIENT HELLO - Sent to SSL HANDLER')
        while self.active:
            local = 0
            complete = False
            marked = False
            ack_number = None
            data, addr = self.s.recvfrom(8000)
            try:
                packet = HeaderParse(data, addr)
                _, tcp_info, dport = packet.Parse()
                if (tcp_info):
                    seq_number, ack_number, tcp_header_length, tcp_segment_length = tcp_info
                ''' Checkign to ensure each packet is part of the same transmission just split over multiple packets '''
                if (ack_number == self.expected_ack_number):
                    print('ACK: {} || EXCPECTED ACK: {} || DPORT {}'.format(ack_number, self.expected_ack_number, dport))
                    if (seq_number in self.ssl_packet):
                        pass
                    elif (seq_number == self.initial_sequence_number):
                        print('SEQ: {} || INITIAL SEQUENCE: {} || DPORT {}'.format(seq_number, self.initial_sequence_number, dport))
                        self.tcp_header_length = tcp_header_length
                        server_hello.update({'status': True, 'sequence': seq_number, 'segment': tcp_segment_length})
                        local = 1
                        marked = True
                    elif (seq_number == self.expected_sequence_number):                       
                        print('SEQ: {} || EXPECTED SEQUENCE: {} || DPORT {}'.format(seq_number, self.expected_sequence_number, dport))
                        marked = True

                    elif (dport == self.client_port):
                        print('OUT OF ORDER PACKET RECIEVED || DPORT {}'.format(dport))
                        marked = True

                    if (marked):
                        self.ssl_packet[seq_number] = data
                        self.expected_sequence_number += tcp_segment_length

                    ''' Identified finished packet, calling method to reorder and remove headers. '''
                    if (self.ssl_packet[seq_number][-4:] == b'\x0e\x00\x00\x00'):
                        hello_done.update({'status': True, 'sequence': seq_number, 'segment': tcp_segment_length})
                        print('DETECTED HELLO DONE')
                    
                    if (server_hello['status'] and hello_done['status']):
                        ('HAVE SERVER H AND H DONE')
                        if (server_hello['sequence'] + server_hello['segment'] == hello_done['sequence']):
                            print('COMPLETE PACKET')
                            complete = True
                        elif (len(self.ssl_packet) >= 3):
                            print('PROBABLY COMPLETE PACKET')
                            complete = True
                        elif (len(self.ssl_packet) == 1 and local == 1):
                            print('SHORT ASS SSL PACKET')
                            complete = True

                        if (complete):
                            print('='*30)
                            ssl_packet = self.FixPacketFormat()
                            ssl = SSLParse(ssl_packet, tcp_header_length)
                            ssl.Start()

                            end = time.time()
                            print('*'*50)
                            print(end-start)
                            print('*'*50)
                            if (ssl.certificate_chain):
                                self.action(packet, ssl)
            except Exception as E:
                print(E)
       
    ''' Getting ascending order of sequence numbers, iterating over packets in order, removing the
    packet headers || Ethernent, IP, TCP || and combining to complete full server hello message '''
    def FixPacketFormat(self):
        ssl_packet_order = sorted(self.ssl_packet.keys())
        header_remove = 34 + self.tcp_header_length

        # for seq_number in ssl_packet_order:
        #     print(seq_number)
        #     print(self.ssl_packet[seq_number])

        ssl_packet = b''
        for seq_number in ssl_packet_order:
            packet = self.ssl_packet[seq_number]
            if (seq_number == self.initial_sequence_number):
                ssl_packet += packet
            else:                
                ssl_packet += packet[header_remove:]
        
#        print(ssl_packet)
        return ssl_packet

    ''' Timeing out Thread after 600 MS to ensure threads do not remain up for invalid or missed traffic '''
    def Timer(self):
        time.sleep(.6)
        self.active = False
                                        
class HeaderParse:
    ''' Class to parse packet header information, including the ssl handshake protocol. All other payloads
    will be ignored. This class isntance will be sent back to the TLS Proxy where it will be able to access all
    class variabled set while parsing to be used for logging purposes or to whitelist/blacklist based on ip or port '''
    def __init__(self, data, addr):
        self.data = data
        self.addr = addr

    def Parse(self):
        hs_type = None
        tcp_info = None
        port = None
#        self.Ethernet()
        self.IP()
        self.Protocol()
        if (self.protocol == 6 and len(self.data) >= 75):
            self.Ports()
            tcp_info = self.TCP()
            if (self.dport == 443):
                self.HandshakeProtocol()
                port = self.sport
                if (self.content_type == 22 and self.handshake_type == 1):
                    hs_type = 1

            elif (self.sport == 443):
                self.HandshakeProtocol()
                port = self.dport
                if (self.content_type == 22 and self.handshake_type == 2):
                    hs_type = 2
                elif (self.content_type == 22 and self.handshake_type == 11):
                    print('TYPE 11 DETECTED')
                            
        return hs_type, tcp_info, port

    ''' Parsing ethernet headers || SRC and DST MAC Address'''            
    def Ethernet(self):
        s = []
        d = []
        smac = struct.unpack('!6c', self.data[0:6])
        dmac = struct.unpack('!6c', self.data[6:12])

        for byte in smac:
            s.append(byte.hex())
        for byte in dmac:
            d.append(byte.hex())
    
        self.smac = '{}:{}:{}:{}:{}:{}'.format(s[0], s[1], s[2], s[3], s[4], s[5])
        self.dmac = '{}:{}:{}:{}:{}:{}'.format(d[0], d[1], d[2], d[3], d[4], d[5])  
    
    ''' Parsing IP headers || SRC and DST IP Address '''
    def IP(self):
        s = struct.unpack('!4B', self.data[26:30])
        d = struct.unpack('!4B', self.data[30:34])
        self.src = '{}.{}.{}.{}'.format(s[0], s[1], s[2], s[3])
        self.dst = '{}.{}.{}.{}'.format(d[0], d[1], d[2], d[3])

    ''' Parsing protocol || TCP 6, UDP 17, etc '''        
    def Protocol(self):
        self.protocol = self.data[23]
        
    ''' Parsing SRC and DST protocol ports '''
    def Ports(self):
        ports = struct.unpack('!2H', self.data[34:38])
        self.sport = ports[0]
        self.dport = ports[1]

    ''' Parsing TCP information like sequence and acknowledgement number amd calculated tcp header
    length to be used by other classes for offset/proper indexing of packet contents.
    Returning all relevant information back to HeaderParse Start method to be redistributed to other classes
    based on need '''
    def TCP(self):
        tcp_header_length = 0
        bit_values = [32,16,8,4]

        tcp = self.data[34:66]
        seq_number = tcp[4:8]
        ack_number = tcp [8:12]
        seq_number = struct.unpack('!L', seq_number)[0]
        ack_number = struct.unpack('!L', ack_number)[0]
        tmp_length = bin(tcp[12])[2:6]

        for i, bit in enumerate(tmp_length):
            if (bit == '1'):
                tcp_header_length += bit_values[i]

        tcp_segment_length = len(self.data) - 34
        tcp_segment_length -= tcp_header_length

        return [seq_number, ack_number, tcp_header_length, tcp_segment_length]

    ''' Parsing SSL Handshake Protocol Types, looking for Type 1 (Client Hello) to start handshake tracking, then
    looking for Type 2 (Server Hello). Handshake Type logical handling is done outside of this method '''
    def HandshakeProtocol(self):
        handshake_protocol = struct.unpack('!B2H2BH', self.data[66:75])
        self.content_type = handshake_protocol[0]
        self.handshake_type = handshake_protocol[3]

class SSLParse:
    ''' This class is to being dealign with the ssl/tls portion of the packet. The entire packet will be looked
    for the first time since prior to this class the packet was split amongth multiple tcp packets. Though the entire
    packet is accessable, all header information will not need to be looked at again due to it already having to be
    parsed to track the connections. This class will be sent into the TLS Proxy where each class object will be accessible
    for further review. As of right now, no additional parsing is being done in this module after this point. Depending on
    how the Proxy side works, additional logic may need to be added here to pinpoint specific variables within the certs
    instead of giving the Proxy the separated certs in their entirety. '''
    def __init__(self, data, tcp_size):
        self.data = data
        self.tcp_size = tcp_size

        self.offset = 0
        self.certificate_offset = 0
        self.certs_combined = 0
        
        self.certificate_chain = []
        self.Parsing = True

    ''' Starting the parsing of the entire packet, focusing on the ssl certificates contained in the packet
    will identify all certificates and append it to a certificate chain list to be more specifilly parsed
    if possible. '''
    def Start(self):
        ''' Calling a recursive method until the certificate handshake protocol type is found which will match
        on type 11, type 2 will apply an offset and pass, all else will be ignored. '''
        while self.Parsing:
            self.HandshakeProtocol()
            if (self.handshake_type == 11):
                self.AllCertificates()
                ''' Calling a recursive method to parse each individual certificate and then checking against total
                expected length. Will set intitial while look condition to False and will break. This will complete
                the certificate chain collection process. '''
                while True:                       
                    self.Certificate_Chain()
                    print('CERTS COMBINED {} : CERTS TOTAL LENGTH {}'.format(self.certs_combined, self.certificates_total_length))
                    if (self.certs_combined == self.certificates_total_length):
                        self.Parsing = False
                        break                  
            elif (self.handshake_type == 2):
                self.offset += self.handshake_type_length + 4 + 5
            else:
                break
    
    ''' Checking the Handshake Protocol Type || 2 (Server Hello), 11 (Certificate) || if Server Hello, implements offset
    to allow for the Certificate to index correctly. If already type 11 (Certificate), then no offset will be applied
    upon returning from this method. '''
    def HandshakeProtocol(self):
        handshake_protocol = struct.unpack('!B2H2BH', self.data[self.offset + 66:self.offset + 75])
        self.content_type = handshake_protocol[0]
        self.version = handshake_protocol[1]
        self.handshake_content_length = handshake_protocol[2]
        self.handshake_type = handshake_protocol[3]
        self.handshake_type_length = handshake_protocol[5]
        
    ''' Parsing the initial certificates fields which contains total length of all certificates which will be used to validate the parsing is complete
    and will also help with indexing the first certificate start location. ''' 
    def AllCertificates(self):
        certificates_length_start = self.offset + self.tcp_size + 34 + 9
        self.certificates_total_length = struct.unpack('!H', self.data[certificates_length_start + 1:certificates_length_start + 3])[0]
        
        certificates_start = certificates_length_start + 3
        self.certificates = self.data[certificates_start:certificates_start+self.certificates_total_length]     

    ''' Checking each individual certificate for the start and end, appending it to a chain list, and setting the certificate offset
    as the length of the current certificate to ensure the next certificate start is indexed correctly '''
    def Certificate_Chain(self):
        certificate_length = struct.unpack('!H', self.certificates[self.certificate_offset+1:self.certificate_offset+3])[0]        
        certificate = self.certificates[self.certificate_offset + 3:self.certificate_offset+certificate_length + 3]                 

        self.certificate_offset += certificate_length + 3        
        self.certs_combined += certificate_length + 3

        self.certificate_chain.append(certificate)
