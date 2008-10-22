import sys
import os
import time
import datetime
import cPickle as pickle

from twisted.internet import protocol, reactor
from twisted.spread import pb

from scrapy import log
from scrapy.core.exceptions import NotConfigured
from scrapy.conf import settings
from scrapy.core.engine import scrapyengine

class ScrapyProcessProtocol(protocol.ProcessProtocol):

    def __init__(self, procman, domain, logfile=None, spider_settings=None):
        self.procman = procman
        self.domain = domain
        self.logfile = logfile
        self.start_time = datetime.datetime.utcnow()
        self.status = "starting"
        self.pid = -1
        self.env = {}
        # We preserve the original settings format for info purposes (avoid
        # lots of unnecesary "SCRAPY_")
        self.scrapy_settings = spider_settings or {}
        self.scrapy_settings.update({'LOGFILE': self.logfile, 
                                     'CLUSTER_WORKER_ENABLED': 0, 
                                     'CLUSTER_CRAWLER_ENABLED': 1, 
                                     'WEBCONSOLE_ENABLED': 0})
        pickled_settings = pickle.dumps(self.scrapy_settings)
        self.env["SCRAPY_PICKLED_SETTINGS_TO_OVERRIDE"] = pickled_settings
        # we nee to pass the worker python path to the crawling process so it
        # knows where to find the local_scrapy_settings
        self.env["PYTHONPATH"] = ":".join(sys.path)

    def __str__(self):
        return "<ScrapyProcess domain=%s, pid=%s, status=%s>" % (self.domain, self.pid, self.status)

    def info(self):
        """Return this scrapy process info as a dict.
        
        The keys are: 

        domain:
          the domain being crawled
        pid:
          the pid of this process
        status:
          the status of this process (starting, running)
        settings:
          the scrapy settings overrided for this process by the worker
        logfile:
          the log file being used
        starttime:
          the start time of this process as a UTC datetime object
        """
        return {"domain": self.domain, 
                "pid": self.pid, 
                "status": self.status, 
                "settings": self.scrapy_settings, 
                "logfile": self.logfile, 
                "starttime": self.start_time}

    def connectionMade(self):
        self.pid = self.transport.pid
        log.msg("ClusterWorker: started domain=%s, pid=%d, log=%s" % (self.domain, self.pid, self.logfile))
        self.transport.closeStdin()
        self.status = "running"
        self.procman.update_master(self.domain, "running")

    def processEnded(self, reason):
        log.msg("ClusterWorker: finished domain=%s, pid=%d, log=%s" % (self.domain, self.pid, self.logfile))
        log.msg("Reason type: %s. value: %s" % (reason.type, reason.value) )
        del self.procman.running[self.domain]
        del self.procman.crawlers[self.pid]
        self.procman.update_master(self.domain, "scraped")

class ClusterWorker(pb.Root):

    def __init__(self):
        if not settings.getbool('CLUSTER_WORKER_ENABLED'):
            raise NotConfigured

        self.maxproc = settings.getint('CLUSTER_WORKER_MAXPROC')
        self.logdir = settings['CLUSTER_LOGDIR']
        self.running = {} # dict of domain->ScrapyProcessControl 
        self.crawlers = {} # dict of pid->scrapy process remote pb connection
        self.starttime = datetime.datetime.utcnow()
        port = settings.getint('CLUSTER_WORKER_PORT')
        scrapyengine.listenTCP(port, pb.PBServerFactory(self))
        log.msg("Using sys.path: %s" % repr(sys.path), level=log.DEBUG)

    def status(self, rcode=0, rstring=None):
        """Return the status of this worker as dict.
        
        The keys of the dict are:
        
        running:
          list of dicts of processes running by this worker. for information
          about the dict see ScrapyProcessControl.status()
        starttime:
          the start time of this worker as a UTC datetime object
        timestamp: 
          the current timestamp as a UTC datetime object
        maxproc: 
          the maximum number of processes supported by this worker
        loadavg: 
          the load average of this worker. see os.getloadavg()
        logdir: 
          the log directory used by this worker
        callresponse: 
          response to the request performed. only available when there was a request
        """

        status = {}
        status["running"] = [self.running[k].info() for k in self.running.keys()]
        status["starttime"] = self.starttime
        status["timestamp"] = datetime.datetime.utcnow()
        status["maxproc"] = self.maxproc
        status["loadavg"] = os.getloadavg()
        status["logdir"] = self.logdir
        status["callresponse"] = (rcode, rstring) if rstring else (0, "No request")
        return status

    def update_master(self, domain, domain_status):
        try:
            deferred = self.__master.callRemote("update", self.status(), domain, domain_status)
        except pb.DeadReferenceError:
            self.__master = None
            log.msg("Lost connection to master", log.ERROR)
        else:
            deferred.addCallbacks(callback=lambda x: x, errback=lambda reason: log.msg(reason, log.ERROR))
        
    def remote_set_master(self, master):
        """Set the master for this worker"""
        log.msg("ClusterWorker: ClusterMaster connected from %s:%s" % master.broker.transport.client)
        self.__master = master
        return self.status()

    def remote_stop(self, domain):
        """Stop a running domain"""
        if domain in self.running:
            proc = self.running[domain]
            log.msg("ClusterWorker: Sending shutdown signal to domain=%s, pid=%d" % (domain, proc.pid))
            d = self.crawlers[proc.pid].callRemote("stop")
            def _close():
                proc.status = "closing"
            d.addCallbacks(callback=_close, errback=lambda reason: log.msg(reason, log.ERROR))
            return self.status(0, "Stopped process %s" % proc)
        else:
            return self.status(1, "%s: domain not running." % domain)

    def remote_status(self):
        """Return worker status as a dict. For infomation about the keys see
        the the status() method""" 
        return self.status()

    def remote_run(self, domain, spider_settings=None):
        """Start scraping the given domain by spawning a process"""
        if len(self.running) < self.maxproc:
            if not domain in self.running:
                logfile = os.path.join(self.logdir, domain, time.strftime("%FT%T.log"))
                if not os.path.exists(os.path.dirname(logfile)):
                    os.makedirs(os.path.dirname(logfile))
                scrapy_proc = ScrapyProcessProtocol(self, domain, logfile, spider_settings)
                args = [sys.executable, sys.argv[0], 'crawl', domain]
                self.running[domain] = scrapy_proc
                try:
                    import pysvn
                    c = pysvn.Client()
                    r = c.update(settings.get("CLUSTER_WORKER_SVNWORKDIR", "."))
                    log.msg("Updated to revision %s." %r[0].number, level=log.DEBUG)
                except pysvn.ClientError, e:
                    log.msg("Unable to svn update: %s" % e, level=log.WARNING)
                except ImportError:
                    log.msg("pysvn module not available.", level=log.WARNING)
                reactor.spawnProcess(scrapy_proc, sys.executable, args=args, env=scrapy_proc.env)
                return self.status(0, "Started process %s." % scrapy_proc)
            else:
                return self.status(2, "Domain %s already running." % domain )
        else:
            return self.status(1, "No free slot to run another process.")

    def remote_register_crawler(self, pid, crawler):
        """Register the crawler to the list of crawlers managed by this worker"""
        self.crawlers[pid] = crawler