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
    logname = ('%03d' % _idx) + '.' + logname
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
    "dynamicConfigClient": { "filepath": "...", "pollInterval": "100s" }
    }

basedynamicconfig = {
    "system.enableEagerWorkflowStart": [ { "value": False } ],
    "frontend.accessHistoryFraction": [ { "value": 1.0 } ],
    "frontend.adminDeleteAccessHistoryFraction": [ { "value": 1.0 } ],
    #"matching.alignMembershipChange": [ { "value" : "10s" } ],
    #"matching.membershipUnloadDelay": [ { "value" : "500ms" } ],
    }

dcpath = lambda env: f"config/dynamicconfig/{env}.yaml"

def make_matching_config(env):
    c = copy.deepcopy(baseconfig)
    c["global"]["pprof"]["port"] = nextport()
    if env != 'rest':
        # prometheus will not scrape this, but it's okay because we only care about history metrics
        c["global"]["metrics"]["prometheus"]["listenAddress"] = f"127.0.0.1:{nextport()}"
    c["services"]["matching"]["rpc"]["grpcPort"] = nextport()
    c["services"]["matching"]["rpc"]["membershipPort"] = nextport()
    c["dynamicConfigClient"]["filepath"] = dcpath(env)
    return c

make_dynamic_config = lambda env: basedynamicconfig

def server(logname, env, services):
    config = make_matching_config(env)
    dynamicconfig = make_dynamic_config(env)
    with open(f"config/{env}.yaml", 'w') as f:
        json.dump(config, f)
    with open(dcpath(env), 'w') as f:
        json.dump(dynamicconfig, f)
    return proc(
        logname, serverwd,
        ['./temporal-server', '--allow-no-auth', f'--env={env}', 'start'] +
        [f'--service={s}' for s in services])

rest = lambda: server(f'rest', 'rest', ['frontend', 'history', 'worker'])
matching = lambda i: server(f'matching-{i}', f'r{i}', ['matching'])

_starter = lambda logname, rps: proc(logname, starterwd, ['go', 'run', './starter', '--rps', str(rps)])
starter = lambda i: _starter(f'starter-{i}', 20)

_worker = lambda logname, pollers, tps: proc(
        logname, workerwd, ['go', 'run', './worker', '--pollers', str(pollers), '--tps', str(tps)])
worker = lambda i: _worker(f'worker-{i}', 2, 35)


def cycle_matching_restart():
    r = rest()
    m1 = matching(1)
    sleep(2)

    # clear out old workflows
    proc('killallworkflows', '.', ['killallworkflows']).wait()

    m2 = matching(2)
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
        log("running")
        sleep(25)

        idx = random.randint(1, len(ms)-1)
        log(f"stopping matching {idx}")
        m = ms[idx]
        m.terminate()
        log(f"waiting for matching {idx}")
        m.wait()

        log(f"running (down one matching)")
        sleep(10)
        log(f"restarting matching {idx}")
        ms[idx] = matching(idx)

    log("running")
    sleep(25)

    log("clearing out")
    s1.terminate()
    s2.terminate()
    sleep(5)

    cleanup()


def set_dc(env, updates):
    # start from base
    dynamicconfig = make_dynamic_config(env)
    for key, val in updates.items():
        if type(val) != list:
            val = [ { "value": val } ]
        dynamicconfig[key] = val
    fn = dcpath(env)
    fntmp = fn + '.tmp'
    with open(fntmp, 'w') as f:
        json.dump(dynamicconfig, f)
    os.rename(fntmp, fn)


def cycle_dynconfig():
    r = rest()
    m1 = matching(1)
    sleep(2)

    # clear out old workflows
    proc('killallworkflows', '.', ['killallworkflows']).wait()

    m2 = matching(2)
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

    val = 0.5
    for i in range(10):
        log("running")

        log("setting sample rate to", val)
        for env in 'rest', 'r1', 'r2', 'r3':
            set_dc(env, {
                'system.validateUTF8.sample.rpcRequest': val,
                'system.validateUTF8.sample.rpcResponse': val,
                'system.validateUTF8.sample.persistence': val,
                })

        val = 0.5 - val

        sleep(120)

    log("clearing out")
    s1.terminate()
    s2.terminate()
    sleep(5)

    cleanup()


def main():
    try:
        cycle_dynconfig()
    finally:
        cleanup()


if __name__ == '__main__':
    main()
