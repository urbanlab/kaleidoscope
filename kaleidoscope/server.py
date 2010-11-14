'''
Kaleidoscope - Server
'''

import sys
import traceback
from kaleidoscope import config
import asyncore, asynchat
import socket
import base64

class KalGameControler(object):
    def __init__(self, controler):
        self.controler = controler
        self.running = False
        self.state = 'idle'
        self.clientcount = 0
        self.reset()

    def reset(self):
        self.scenarioname = 'choose'
        self.scenario = None
        self.status = {}

    def tick(self):
        '''State machine of the game, call the appropriate state
        '''

        # ensure the game can go, if they are at least one client.
        if len(self.controler.clients) == 0:
            if self.state != 'idle':
                print '# All clients have leaved, reset to idle.'
                self.state = 'idle'
                self.reset()

        try:
            funcname = 'handle_%s' % self.state
            getattr(self, funcname)()
        except Exception, e:
            print 'KalGameControler error at', funcname
            print e

    def update_client_status(self, client, status):
        '''Update status of a client
        '''
        self.status[client] = status.lower()

    def client_receive(self, client, args):
        '''Send data from client to game
        '''
        self.scenario.client_receive(client, args)

    def handle_idle(self):
        '''Default handler when nothing is done.
        Just wait for few peoples to come in the game.
        '''
        l = len(self.controler.clients)
        if l <= 0:
            return
        self.state = 'load'
        self.clientcount = l

    def switch_scenario(self, scenario):
        self.scenarioname = scenario
        self.handle_load()
        for client in self.controler.clients:
            self.load(client)

    def handle_load(self):
        try:
            package = 'kaleidoscope.scenarios.%s' % self.scenarioname
            for x in sys.modules.keys()[:]:
                if x.startswith(package):
                    del sys.modules[x]

            pack = __import__(package, fromlist=['server'])
            self.scenario = pack.server.scenario_class(self)
        except Exception, e:
            self.controler.notify_all('Server error while trying to load scenario')
            self.controler.notify_all('Game cancelled.')
            self.state = 'idle'
            traceback.print_exc()
            return
        self.state = 'running'

    def load(self, client):
        if self.state == 'idle':
            self.handle_load()
        try:
            self.controler.send_to(client, 'LOAD %s\n' % self.scenarioname)
            for x in self.scenario.resources:
                self.controler.send_to(client, 'REQUIRE %s %s %s\n' % (
                    self.scenarioname, x, self.scenario.sha224(x)))
        except Exception, e:
            self.controler.notify_all('Server error while trying to load scenario', client)
            self.controler.notify_all('Game cancelled.', client)
            traceback.print_exc()
            return

    def handle_running(self):

        # Sync part
        name = self.controler.get_client_name
        for client in self.controler.clients:
            issync = True
            if client not in self.status:
                print '# Waiting status of @%s' % name(client)
                self.controler.send_to(client, 'SYNC %s\n' % self.scenarioname)
                self.status[client] = '__init__'
                continue

            status = self.status[client]
            if status == 'done':
                continue
            elif status == 'wait requirement':
                self.controler.send_to(client, 'SYNC %s\n' % self.scenarioname)
            elif status == 'ready':
                self.status[client] = 'done'
                self.scenario.client_login(client)
            else:
                print '# @%s is still %s' % (name(client), status)

        # Scenario part
        try:
            self.scenario.update()
        except:
            traceback.print_exc()


class KalControler(object):
    instance = None

    @staticmethod
    def get_instance():
        if KalControler.instance is None:
            KalControler.instance = KalControler()
        return KalControler.instance

    def __init__(self):
        super(KalControler, self).__init__()
        self.clients = {}
        self.game = KalGameControler(self)

    def get_client_name(self, client):
        return self.clients[client]

    def tick(self):
        '''State machine of the game :)
        '''
        self.game.tick()

    def raw(self, client, message):
        '''Send a raw message
        '''
        try:
            print '<', client.addr, message.replace('\n', '')
            client.push(message)
        except:
            print '# Fatal error while sending %s to %s' % (message, client)

    def failed(self, client, message):
        '''Return a error message to the client
        '''
        self.raw(client, 'FAILED %s\n' % message)

    def ok(self, client, message=None):
        '''Return a ok message to the client
        '''
        if message:
            self.raw(client, 'OK %s\n' % message)
        return self.raw(client, 'OK\n')

    def send_to(self, client, message):
        self.raw(client, message)

    def send_all(self, message, client=None):
        for cli in self.clients:
            if client and cli is client:
                continue
            self.raw(cli, message)

    def notify_all(self, message, client=None):
        self.send_all('NOTIFY %s\n' % message, client)

    def handle_from(self, client, cmd, args):
        '''Handle a command from a client + ensure that the client is known.
        '''
        if client not in self.clients and self.game.running:
            return self.failed(client, 'A game is running')

        try:
            func = getattr(self, 'handle_%s' % cmd.lower())
            func(client, args)
        except Exception, e:
            self.failed(client, 'Invalid command <%s>' % cmd.lower())
            traceback.print_exc()

    def handle_login(self, client, args):
        '''Subscribe to the game, if possible.
        '''
        if client in self.clients:
            return self.failed(client, 'Already subscribed')
        if len(args) <= 2:
            return self.failed(client, 'Invalid login')
        self.clients[client] = args
        self.notify_all('@%s est en ligne' % args)
        self.ok(client)
        if self.game:
            self.game.load(client)

    def handle_logout(self, client, args):
        if client not in self.clients:
            return
        clientname = self.clients[client]
        del self.clients[client]
        self.notify_all('@%s est parti' % clientname)
        if self.game and self.game.scenario:
            self.game.scenario.client_logout(client)

    def handle_status(self, client, args):
        self.game.update_client_status(client, args)

    def handle_game(self, client, args):
        self.game.client_receive(client, args)

    def handle_get(self, client, args):
        scenarioname, filename = args.split()
        if scenarioname != self.game.scenarioname:
            self.failed(client, 'Invalid scenario name for GET')
            return
        data = self.game.scenario.get(filename)
        data = base64.urlsafe_b64encode(data)
        self.send_to(client, 'WRITE %s %s %s\n' % (scenarioname, filename, data))


class KalServerChannel(asynchat.async_chat):

    def __init__(self, server, sock, addr):
        asynchat.async_chat.__init__(self, sock)
        self.set_terminator("\n")
        self.request = None
        self.data = ''
        self.shutdown = 0

    def collect_incoming_data(self, data):
        self.data = self.data + data

    def found_terminator(self):
        out = self.data.split(None, 1)
        if len(out) == 0:
            return
        if len(out) == 2:
            cmd, args = out
        else:
            cmd = out[0]
            args = []
        self.dispatch_command(cmd, args)
        self.data = ''

    def dispatch_command(self, cmd, args):
        print '>', self.addr, cmd, args
        KalControler.get_instance().handle_from(self, cmd, args)

    def handle_close(self):
        print '# Client disconnected', self.addr
        self.dispatch_command('LOGOUT', ())
        return asynchat.async_chat.handle_close(self)

class KalServer(asyncore.dispatcher):

    def __init__(self, host, port):
        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.bind((host, port))
        self.listen(4)

    def handle_accept(self):
        conn, addr = self.accept()
        print '# Client connected', addr
        KalServerChannel(self, conn, addr)

if __name__ == '__main__':
    print '# Start Kaleidoscope server at', (config.server_ip, config.server_port)
    s = KalServer(config.server_ip, config.server_port)
    try:
        while True:
            asyncore.loop(timeout=.05, count=1)
            KalControler.get_instance().tick()
    except:
        print '# Close the socket...'
        s.close()
        raise
