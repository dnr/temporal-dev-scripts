#!/usr/bin/env python

import sys, os, datetime, subprocess, random, copy, json
from time import sleep

serverwd = '/home/dnr/t/temporal'
starterwd = workerwd = '/home/dnr/t/samples-go/repro'

outbase = os.path.join(serverwd, f'run.out.{datetime.datetime.now().isoformat("T", "seconds").replace(":","")}')
os.makedirs(outbase)

_idx = 0
_port = 30000
liveprocs = []

def makelog(logname):
    global _idx
    logname = ('%05d' % _idx) + '.' + logname
    _idx += 1
    return open(os.path.join(outbase, logname), 'w', buffering=1)

def nextport():
    global _port
    _port += 1
    return _port

glog = makelog("test")
def log(*args, **kwargs):
    now = datetime.datetime.now().isoformat()
    print(now, *args, **kwargs, file=glog)
    print(now, *args, **kwargs)

def proc(logname, wd, args):
    global liveprocs
    out = makelog(logname)
    log('running:', ' '.join(args))
    p = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, cwd=wd)
    liveprocs.append(p)
    return p

def cleanup():
    global liveprocs
    log('cleaning up...')
    liveprocs = [p for p in liveprocs if p.poll() is None]
    for p in liveprocs: p.terminate()
    while liveprocs:
        sleep(0.5)
        liveprocs = [p for p in liveprocs if p.poll() is None]


baseconfig = {
    "log": { "stdout": True, "level": "info" },
    "persistence": {
        "defaultStore": "cass-default", "visibilityStore": "es-visibility", "numHistoryShards": 4,
        "datastores": {
            "cass-default": { "cassandra": { "hosts": "127.0.0.1", "keyspace": "temporal" } },
            "es-visibility": {
                "elasticsearch": {
                    "version": "v7", "logLevel": "error",
                    "url": { "scheme": "http", "host": "127.0.0.1:9200" },
                    "indices": { "visibility": "temporal_visibility_v1_dev" },
                    "closeIdleConnectionsInterval": "15s"
                    } } } },
    "global": {
        "membership": { "maxJoinDuration": "30s", "broadcastAddress": "127.0.0.1" },
        "pprof": { "port": 9936 },
        "metrics": { "prometheus": { "framework": "tally", "timerType": "histogram", "listenAddress": "127.0.0.1:8000" } }
        },
    "services": {
        "frontend": { "rpc": { "grpcPort": 7233, "membershipPort": 6933, "bindOnLocalHost": True, "httpPort": 7243 } },
        "matching": { "rpc": { "grpcPort": 9235, "membershipPort": 9935, "bindOnLocalHost": True } },
        "history": { "rpc": { "grpcPort": 7234, "membershipPort": 6934, "bindOnLocalHost": True } },
        "worker": { "rpc": { "grpcPort": 7239, "membershipPort": 6939, "bindOnLocalHost": True } }
        },
    "clusterMetadata": {
        "enableGlobalNamespace": False, "failoverVersionIncrement": 10, "masterClusterName": "active", "currentClusterName": "active",
        "clusterInformation": { "active": { "enabled": True, "initialFailoverVersion": 1, "rpcName": "frontend", "rpcAddress": "localhost:7233" } }
        },
    "dcRedirectionPolicy": { "policy": "noop" },
    "archival": { "history": { "state": "disabled" }, "visibility": { "state": "disabled", } },
    "namespaceDefaults": { "archival": { "history": { "state": "disabled" }, "visibility": { "state": "disabled" } } },
    "dynamicConfigClient": { "filepath": "config/dynamicconfig/development-cass.yaml", "pollInterval": "10s" }
    }

def make_matching_config():
    c = copy.deepcopy(baseconfig)
    c["global"]["pprof"]["port"] = nextport()
    # prometheus will not scrape this, but it's okay because we only care about history metrics
    c["global"]["metrics"]["prometheus"]["listenAddress"] = f"127.0.0.1:{nextport()}"
    c["services"]["matching"]["rpc"]["grpcPort"] = nextport()
    c["services"]["matching"]["rpc"]["membershipPort"] = nextport()
    return c

def server(logname, env, services, config):
    with open(f"config/{env}.yaml", 'w') as f:
        json.dump(config, f)
    return proc(
        logname, serverwd,
        ['./temporal-server', '--allow-no-auth', f'--env={env}', 'start'] +
        [f'--service={s}' for s in services])

rest = lambda: server(f'rest', 'rest', ['frontend', 'history', 'worker'], baseconfig)
matching = lambda i: server(f'matching-{i}', f'r{i}', ['matching'], make_matching_config())

_starter = lambda logname, rps: proc(logname, starterwd, ['go', 'run', './starter', '--rps', str(rps)])
starter = lambda i: _starter(f'starter-{i}', 20)

_worker = lambda logname, pollers, tps: proc(
        logname, workerwd, ['go', 'run', './worker', '--pollers', str(pollers), '--tps', str(tps)])
worker = lambda i: _worker(f'worker-{i}', 2, 35)


def cycle():
    r = rest()
    m1 = matching(1)
    sleep(2)
    m2 = matching(2)
    sleep(2)
    m3 = matching(3)
    ms = [None, m1, m2, m3]

    log("sleeping before worker start")
    sleep(2)

    w1 = worker(1)
    w2 = worker(2)

    log("sleeping before starter start")
    sleep(2)

    s1 = starter(1)
    s2 = starter(2)

    for i in range(10):
        log("waiting")
        sleep(25)

        idx = random.randint(1, len(ms)-1)
        log(f"stopping matching {idx}")
        ms[idx].terminate()

        log(f"waiting (down one matching)")
        sleep(10)
        log(f"restarting matching {idx}")
        ms[idx] = matching(idx)

    log("waiting")
    sleep(25)

    log("clearing out")
    s1.terminate()
    s2.terminate()
    sleep(5)

    cleanup()


def main():
    try:
        cycle()
    finally:
        cleanup()


if __name__ == '__main__':
    main()
