# Copyright (c) 2021 MIT
# 
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR(S) DISCLAIM ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL AUTHORS BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

import time
import signal
import sys, os
import subprocess
import json
import xmlrpc.server
import xmlrpc.client
import re
import threading
from os.path import expanduser
from argparse import ArgumentParser, REMAINDER
from typing import Optional, IO, List, Any
from jobDescription import TrainingJob
import grpc
import runtime_pb2
import runtime_pb2_grpc

import torch

# import examples.vgg as vgg  # TODO: this is used for debugging. Remove this later.

extra_args = [] # unparsed arguments stored here are forwarded to runtimes

HAS_EXCEPTION = False
def excepthook(args):
    global HAS_EXCEPTION
    print("In excepthook", args)
    HAS_EXCEPTION = True

threading.excepthook = excepthook

def waitthreads(threadList):
    for thread in threadList:
        while thread.is_alive() and not HAS_EXCEPTION:
            time.sleep(0.1)
        if HAS_EXCEPTION:
            sys.exit(-1)
        thread.join()

class CppRuntimeProxy:
    def __init__(self, addressWithPort: str):
        self.channel = grpc.insecure_channel(addressWithPort) # ex) 'localhost:50051'
        self.stub = runtime_pb2_grpc.RuntimeStub(self.channel)

    def scheduleTraining(self, name, jobInJson, dataDir, tensorTagsInJson, jobRankToGlobalRankInJson, jobParamsInJson):
        response = self.stub.ScheduleTraining(runtime_pb2.ScheduleTrainingRequest(
            name=name, job_in_json=jobInJson, data_dir=dataDir,
            tensor_tags_in_json=tensorTagsInJson,
            job_rank_to_global_rank_in_json=jobRankToGlobalRankInJson, job_meta_params_in_json=jobParamsInJson))
        print("received: " + response.message)
    
    def poke(self):
        response = self.stub.Poke(runtime_pb2.Empty())
        # print("received: " + response.message)

    def shutdown(self):
        response = self.stub.Shutdown(runtime_pb2.Empty())
        print("received: " + response.message)

    def initCommBackend(self):
        # response = self.stub.(runtime_pb2.Empty())
        # print("received: " + response.message)
        pass
        # print("initCommBackend() not implemented")

    def initCommNCCL(self, message, msgType, groupId, members):
        response = self.stub.InitCommNCCL(runtime_pb2.InitCommNCCLMsg(
            message=message, msg_type=msgType, group_id=groupId, members=members))
        print("received: " + response.message)
        return response.group_id;

    def initCommGRPC(self, rankToIpMap):
        rankToIpMapInJson = json.dumps(rankToIpMap)
        print("In initCommGRPC, rankToIpMapInJson: " + rankToIpMapInJson)
        response = self.stub.InitCommGRPC(runtime_pb2.InitCommGRPCRequest(
            rank_to_ip_map_in_json = rankToIpMapInJson
        ))
        print("received: " + response.message)

    def initCommGroups(self, jobName, commGroupsInJson):
        print("initCommGroups not implemented")


class Location:
    def __init__(self, address: str, port: int, device: int, userId: str, sshKeyPath: str, isCpp: bool):
        self.address = address
        self.port = port
        self.device = device
        self.userId = userId
        self.sshKeyPath = sshKeyPath
        self.serverId = None
        self.proxy = None
        self.isCpp = isCpp
        self.is_local = address == "127.0.0.1"
        self.process = None

    def getProxy(self, maxRetry = 180):
        if self.proxy != None:
            # print("getProxy() returned from cached proxy value.")
            return self.proxy

        # Python runtime
        retryGap = 1
        retryCount = 0
        while retryCount < maxRetry:
            try:
                if self.isCpp: # CPP runtime
                    self.proxy = CppRuntimeProxy("%s:%d"%(self.address, self.port))
                    # print("cppProxy created for %s:%d"%(self.address, self.port))
                else:
                    self.proxy = xmlrpc.client.ServerProxy("http://%s:%d/"%(self.address, self.port))
                self.proxy.poke()
                return self.proxy
            except (ConnectionRefusedError, grpc.RpcError): # ConnectionRefusedError is for xmlrpc.
                print("Cannot connect to %s:%d. Will retry in %d sec." %
                    (self.address, self.port, retryGap))
                time.sleep(retryGap)
                # retryGap += 2 # exponential back off.
                retryCount += 1
        assert False, "couldn't connect"
        return None

    def downloadFile(self, remotePath: str, localPath: str):
        assert not self.is_local
        print("  Downloading %s to %s at %s" % (remotePath, localPath, self.address))
        kwargs = dict()
        kwargs['stderr'] = subprocess.STDOUT
        # sh_command = ['mkdir', '-p', localPath]
        # subprocess.check_call(sh_command, **kwargs)
        sh_command = ['scp', '-i', self.sshKeyPath, '%s@%s:%s' % (self.userId, self.address, remotePath), localPath]
        subprocess.check_call(sh_command, **kwargs)

    def uploadFile(self, localFilePath, remotePath):
        assert not self.is_local
        print("  Uploading %s to %s at %s" % (localFilePath, remotePath, self.address))
        kwargs = dict()
        # kwargs['shell'] = True
        kwargs['stderr'] = subprocess.STDOUT
        sh_command = ['scp', '-i', self.sshKeyPath, localFilePath, '%s@%s:%s' % (self.userId, self.address, remotePath)]
        subprocess.check_call(sh_command, **kwargs)
    
    def rsh(self, command):
        kwargs = dict()
        kwargs['stderr'] = subprocess.STDOUT
        
        # sh_command = ['ssh', '-v', '-i', '~/.ssh/ulma-sjp.pem', 'ubuntu@%s' % self, '%s' % command]
        if self.is_local:
            sh_command = command
            kwargs["shell"] = True
        else:
            sh_command = ['ssh', '-i', self.sshKeyPath, '-o', 'StrictHostKeyChecking=no', '%s@%s' % (self.userId, self.address), '%s' % command]
        try:
            subprocess.check_call(sh_command, **kwargs)
        except subprocess.CalledProcessError as e:
            output = e.output
            exit(1)
        return
    
    def __monitor(self):
        self.process.wait()
        sys.exit(0)

    def rshAsync(self, command, **kwargs):
        print("Sending cmd: %s" % command)
        if self.is_local:
            sh_command = command
            kwargs["shell"] = True
        else:
            sh_command = ['ssh', '-i', self.sshKeyPath, '-o StrictHostKeyChecking=no', '%s@%s' % (self.userId, self.address),
                    '%s' % command]
        self.process = subprocess.Popen(sh_command, **kwargs)
        t = threading.Thread(target=Location.__monitor, args=(self,), daemon=True)
        t.start()
        return self.process

    def upSync(self, localPath, remotePath):
        if self.is_local:
            assert False
            return
        try:
            subprocess.check_call(['rsync', '-e', 'ssh -i %s -o StrictHostKeyChecking=no' % self.sshKeyPath,
                '-rh', "--exclude=*__pycache__", localPath, "%s@%s:%s" % (self.userId, self.address, remotePath)],
                stderr=subprocess.STDOUT)
        except subprocess.CalledProcessError as e:
            output = e.output
            exit(1)

class ClusterCoordinator(xmlrpc.server.SimpleXMLRPCServer):
    """ GPU cluster coordinator. It accepts training jobs from clients and schedule them to runtimes. """

    def __init__(self, addrToBind: str, portToBind: int, locations: List[Location], workDir: str, be_batch_size: int):
        super(ClusterCoordinator, self).__init__((addrToBind, portToBind))
        self.myAddr = addrToBind
        self.myPort = portToBind
        self.locations = locations
        self.workDir = workDir
        self.processes = []  # from subprocess calls used for launching runtime.
        self.nextTagStartOffset = 1
        self.be_batch_size = be_batch_size
        self.commGroups = set()
        self.ongoingJobs = {} # Dict of contexts of ongoing jobs. Indexed by job name.
        f = open("runtimeResult.data", "w")
        f.close()
        
    
    def _dispatch(self, method, params):
        """ Custom dispatcher for XML-RPC server. """
        try:
            # We are forcing the 'export_' prefix on methods that are
            # callable through XML-RPC for security.
            func = getattr(self, 'export_' + method)
        except AttributeError:
            raise Exception('method "%s" is not supported' % method)
        else:
            return func(*params)

    ######################################################
    ## RPC handlers
    ######################################################
    def export_poke(self):
        return 'Returned from poke at %s' % self.myAddr

    def export_scheduleTraining(self, jobName: str, trainingJobInJSON: str, runbe):
        job = TrainingJob("test", None, None, 0, 0, "")
        job.loadJSON(trainingJobInJSON)
        print("received job")
        
        gpusUsed = job.getGpusUsed()
        moduleDescList = [job.dumpSingleRunnableModule(rank) for rank in range(gpusUsed)]
        tensorTags = self.buildCommTensorTags(moduleDescList)
        tensorTagsInJson = json.dumps(tensorTags)
        for rank in range(gpusUsed):
            with open(f"/tmp/rank{rank}.json", "wb") as f:
                f.write(bytes(moduleDescList[rank].encode("utf-8")))

        commSets = self.buildNeededCommGroups(moduleDescList)

        for s in commSets:
            self.initCommBackendAll("nccl", s)

        jobRankToGlobalRank = list(range(gpusUsed))
        jobRankToGlobalRankInJson = json.dumps(jobRankToGlobalRank)

        # TODO: should pick locations that doesn't have other priority job scheduled.
        if len(self.locations) < gpusUsed:
            return "Not enough servers available. %d gpus available while %d needed" % (len(self.locations), gpusUsed)

        jobParams = {
            "run_with_be": runbe,
            "nr_gpus": gpusUsed,
            "cifar_training": "cifar" in jobName,
            "lossfn": "CrossEntropyLoss" if "gpt2" in jobName else "NLL",
        }

        jobParamsInJson = json.dumps(jobParams)

        threadList = []
        def requestScheduleTraining(proxy, jobInJson):
            proxy.scheduleTraining(jobName, jobInJson, "SYNTHETIC", tensorTagsInJson, jobRankToGlobalRankInJson, jobParamsInJson)
        for rank in range(gpusUsed):
            location = self.locations[rank]
            moduleDesc = moduleDescList[rank]
            thread = threading.Thread(name='reqScheTrain%d'%rank, target=requestScheduleTraining, args=(location.getProxy(), moduleDesc))
            threadList.append(thread)
            thread.start()
        waitthreads(threadList)

        self.ongoingJobs[jobName] = {"iterTime": 0, "gpuMsec": 0, "gpusUsed": gpusUsed, "gpusFinished": 0, "globalBatchSize": job.globalBatchSize}
        self.ongoingJobs[jobName].update({"beImagesPerIter": 0.0, "idleMsPerIter": 0.0})

        # for rank in range(gpusUsed):
        #     location = self.locations[rank]
        #     moduleDesc = moduleDescList[rank] # job.dumpSingleRunnableModule(rank)
        #     print(location.getProxy().scheduleTraining(jobName, moduleDesc, "SYNTHETIC", tensorTagsInJson, jobRankToGlobalRankInJson))
        return 'done'

    def export_notifyTrainingFinished(self, runtimeAddress: str, name: str, beImagesPerIter: float, idleMsPerIter: float, remainingJobCount: int, fpTime: float, bpTime: float, iterTime: float):
        print("Training for %s is completed at %s. (%d jobs are remaining) fp: %3.1f bp: %3.1f iterTime: %3.1f" % (name, runtimeAddress, remainingJobCount, fpTime, bpTime, iterTime))
        iterTime /= 1000
        self.ongoingJobs[name]["iterTime"] = max(self.ongoingJobs[name]["iterTime"], iterTime)
        self.ongoingJobs[name]["gpuMsec"] += (fpTime + bpTime) / 1000
        self.ongoingJobs[name]["gpusFinished"] += 1
        self.ongoingJobs[name]["beImagesPerIter"] += beImagesPerIter
        self.ongoingJobs[name]["idleMsPerIter"] += idleMsPerIter
        if self.ongoingJobs[name]["gpusFinished"] == self.ongoingJobs[name]["gpusUsed"]:
            toprints = [
                "{globalBatchSize:2}", "{gpusUsed:2}", "{iterTime:4.1f}",
                "{gpuMsec:4.1f}", "{beImagesPerIter:3.1f}",
                "{idleMsPerIter:3.1f}"
            ]
            print("Training for {} is completed entirely.".format(name))
            cols = ["GlobalBatchSize", "GpusUsed", "IterTime", "GpuMsec", "BeImagesPerIter", "IdleMsPerIter"]
            print("  " + "    ".join(cols))
            dataline = "  " + "    ".join(toprints).format(**self.ongoingJobs[name])
            print(dataline)
            f = open("runtimeResult.data", "a")
            f.write(dataline + "\n")
            f.close()
        return 'done'

    def export_addGpuNode(self):
        print("NOT YET IMPLEMENTED.")

    ######################################################
    ## Internal helper methods
    ######################################################
    def buildCommTensorTags(self, moduleDescList):
        # TODO: need tag allocator that can recycle tags.
        tag = 0
        tensorTags = {}
        for moduleDesc in moduleDescList:
            spec = json.loads(moduleDesc)
            for ldsc in spec["layers"]:
                if "xfers" in ldsc: # either sender or receiver need to assign tag.
                    for item in ldsc["xfers"]:
                        tensorTags[item["name"]] = tag
                        tag += item["prop"]["xferSamples"]
                        tensorTags[item["name"] + "_back"] = tag
                        tag += item["prop"]["xferSamples"]
        return tensorTags

    def buildNeededCommGroups(self, moduleDescList):
        groups = set()
        desc = json.loads(moduleDescList[0])
        for l in desc['layers']:
            activeset = tuple(sorted(l['gpuAssignment']))
            if len(activeset) > 1:
                groups.add(activeset)
        return list(groups)

    ######################################################
    ## Runtime cluster management
    ######################################################
    def installPackages(self):
        """ Install required software at each runtime server """
        pipPackages = ["torch", "jsonpickle", "torchvision"]
            # "pip install torch==1.8.0+cu111 torchvision==0.9.0+cu111 torchaudio==0.8.0 -f https://download.pytorch.org/whl/torch_stable.html"]
        for location in self.locations:
            for pipPackage in pipPackages:
                location.rsh("pip install %s" % pipPackage)
    def launchRuntimeAll(self, c10dBackend: str, profile: bool, cppRuntime: bool, manualLaunch: bool):
        """ Launch runtime at all remote locations. Also registers the sighandler
            that cleanly shuts down all remote runtime servers.
        """

        # Using the absolute path for compatibility with C++ runtime.
        logdir = args.logdir
        if not logdir:
            logdir = os.getcwd() + "/logs/"
        upSyncedAddrs = set()
        for i, location in enumerate(self.locations):
            if (location.address not in upSyncedAddrs):
                # TODO: skip if location's addr is same as the current node.
                # location.upSync(".", self.workDir)
                upSyncedAddrs.add(location.address)

            # pass master ip and port.
            stdoutFp = open(f"{logdir}/runtime%d.out"%i, "a", buffering=1)
            stderrFp = open(f"{logdir}/runtime%d.err"%i, "a", buffering=1)
            nsysPrefix = ""
            if "--cuda_profile" in extra_args:# and location.device == 0: # Only run 1 nsys per host.
                nsysPrefix = "nsys profile -f true -o net%d -c cudaProfilerApi -t cuda,nvtx --export sqlite " % i # -s none
            if manualLaunch:
                print("Skipping ssh launching runtime. Must have launched them manually.")
            elif cppRuntime:
                self.processes.append(location.rshAsync(
                    f"CUDA_VISIBLE_DEVICES={location.device} {nsysPrefix} {self.workDir}/csrc/build/runtime" + \
                    " --myAddr %s:%d --device 0 --c10dBackend %s --rank %d --worldSize %d --logdir %s --be_batch_size %d %s" % \
                        (location.address, location.port, c10dBackend, i, len(self.locations), logdir, self.be_batch_size, " ".join(extra_args)) #+ \
                    , stdout=stdoutFp, stderr=stderrFp))
            else:
                self.processes.append(location.rshAsync(
                    # nsysPrefix + "python3 " + self.workDir + "runtime.py" + \
                    "source ~/.profile; " +  nsysPrefix + "python3 " + self.workDir + "runtime.py" + \
                    " --coordinatorAddr %s:%d --myAddr %s:%d --device %d --c10dBackend %s --rank %d --worldSize %d --be_batch_size %d %s" % \
                        (self.myAddr, self.myPort, location.address, location.port, location.device, c10dBackend, i, len(self.locations), self.be_batch_size, "--profile" if profile else "") #+ \
                    , stdout=stdoutFp, stderr=stderrFp))

            sig_names = {2: "SIGINT", 15: "SIGTERM"}
            last_return_code = None
            def sigkill_handler(signum, frame):
                print("signum:%d Trying to shutdown all runtime." % signum)
                self.shutdownRuntimeAll()
                # self.waitForRuntimeAll()
                for process in self.processes:
                    print(f"Killing subprocess {process.pid}")
                    try:
                        process.terminate()
                        # process.kill()
                    except Exception:
                        pass
                if last_return_code is not None:
                    raise subprocess.CalledProcessError(returncode=last_return_code, cmd=cmd)
                if signum in sig_names:
                    print(f"Main process received {sig_names[signum]}, exiting")
                sys.exit(1)
            signal.signal(signal.SIGINT, sigkill_handler)
            # signal.signal(signal.SIGTERM, sigkill_handler)
        
        time.sleep(2) ## + (15 if profile else 0))
        for location in self.locations:
            proxy = location.getProxy()
            proxy.poke()

    def shutdownRuntimeAll(self):
        """ Ask all remote runtime servers to stop. Returns after all servers ack the shutdown request. """
        for location in self.locations:
            try:
                proxy = location.getProxy(maxRetry=1)
                if proxy != None:
                    print(proxy.shutdown())
                # print(location.getProxy(maxRetry=1).shutdown())
            except xmlrpc.client.Fault:
                print("pipe broken while shuting down %s" % location.address)
            except grpc.RpcError:
                print("GRPC error while shuting down %s" % location.address)

    def initCommBackendAll(self, c10dBackend, commGroupSet):
        assert(sorted(commGroupSet) == list(commGroupSet))
        if tuple(commGroupSet) in self.commGroups:
            return

        self.commGroups.add(tuple(commGroupSet))

        if c10dBackend == "nccl":
            group_id = self.locations[commGroupSet[0]].getProxy().initCommNCCL("Generate comm group ID", 0, bytes(128), list(commGroupSet))
        threadList = []
        def requestInitCommBackend(proxy):
            # print(proxy.initCommBackend())
            if c10dBackend == "grpc":
                print(proxy.initCommGRPC(rankToIpMap))
            if c10dBackend == "nccl":
                proxy.initCommNCCL("Join comm group", 1, group_id, list(commGroupSet))

        for i in commGroupSet:
            location = self.locations[i]
            thread = threading.Thread(name='init_comm%d'%i, target=requestInitCommBackend, args=(location.getProxy(),))
            thread.start()
            threadList.append(thread)
        waitthreads(threadList)

    def initCommGroupsAll(self, jobName: str, commGrpDict: dict, jobRankToGlobalRank: list):
        """ A helper function that will ask all runtimes to create new c10d comm groups.
            Used while scheduling a new training job. This method should be invoked before
            scheduling a new training job to any runtime that will participate in training.
        """

        commGrpDictWithGlobalRanks = {}
        for grpName in commGrpDict:
            grpRanks = commGrpDict[grpName]
            globalGrpRanks = [jobRankToGlobalRank[rank] for rank in grpRanks]
            commGrpDictWithGlobalRanks[grpName] = globalGrpRanks
        commGrpDictWithGlobalRanksInJson = json.dumps(commGrpDictWithGlobalRanks)

        threadList = []
        def requestInitCommGroups(proxy, jobName, commGroupsInJson):
            # print(proxy.initCommGroups(jobName, commGroupsInJson))
            proxy.initCommGroups(jobName, commGroupsInJson)
        for i, location in enumerate(self.locations):
            thread = threading.Thread(name='init_commGroups%d'%i, target=requestInitCommGroups,
                                      args=(location.getProxy(), jobName, commGrpDictWithGlobalRanksInJson,))
            thread.start()
            threadList.append(thread)
        waitthreads(threadList)
            

    def waitForRuntimeAll(self):
        """ Waits until all runtime processes terminate. Development use only. """
        # TODO: replace this method with xmlrpc server event loop.
        print("Waiting for ssh process to terminate.")
        for p in self.processes:
            p.wait()


####################################################################################
##  Initial launch scripts
####################################################################################

def parse_args():
    """
    Helper function parsing the command line options
    @retval ArgumentParser
    """
    parser = ArgumentParser(description="ClusterCoordinator initial launch "
                                        "script that will spawn up "
                                        "multiple distributed processes")

    # Optional arguments for the launch helper
    parser.add_argument("--addrToBind", type=str, default="localhost:12340",
                        help="IP:port to listen for requests to the cluster coordinator")
    parser.add_argument("--c10dBackend", type=str, default="nccl",
                        help="pytorch c10d communication backend. Type either nccl or gloo")
    parser.add_argument("--logLevel", type=int, default=1,
                        help="Logging level. 0: verbose, 1: Info, 2: Error") # NOT YET IMPLEMENTED.
    parser.add_argument("--pathToConfig", type=str, default="clusterConfig.json",
                        help="The full path to the cluster configuration files")
    parser.add_argument('--install', default=False, action='store_true',
                        help="When this option is set, it will install required pip packages to all servers")
    parser.add_argument('--profile', default=False, action='store_true',
                        help="To launch runtimes with night system profiling.")
    parser.add_argument("--be_batch_size", type=int, default=0,
                        help="launch runtimes with be beatch size")
    parser.add_argument('--cpp', default=False, action='store_true',
                        help="To launch CPP version runtimes.")
    parser.add_argument('--manualLaunch', default=False, action='store_true',
                        help="Do not runtimes automatically. Primarily for using gdb on runtime processes.")
    parser.add_argument("--logdir", type=str, default="", help="Full path of log directory")
    # For installing nsys.. (with other cuda toolkit..)
    # wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu1804/x86_64/cuda-ubuntu1804.pin
    # sudo mv cuda-ubuntu1804.pin /etc/apt/preferences.d/cuda-repository-pin-600
    # sudo apt-key adv --fetch-keys https://developer.download.nvidia.com/compute/cuda/repos/ubuntu1804/x86_64/7fa2af80.pub
    # sudo add-apt-repository "deb https://developer.download.nvidia.com/compute/cuda/repos/ubuntu1804/x86_64/ /"
    # sudo apt-get update
    # sudo apt-get -y install cuda

    return parser.parse_known_args()

def main():
    global args, extra_args
    args, extra_args = parse_args()
#    clusterConfig = json.load(open(args.pathToConfig))
    global rankToIpMap
    rankToIpMap = {}
    commGrpRanksWorld = []
    locations = []
#    for serverConfig in clusterConfig["serverList"]:
#        print("Found %s" % str(serverConfig))
    port = 11250
    for i in range(torch.cuda.device_count()):
        rankToIpMap[str(len(locations))] = f"127.0.0.1:{port}"
        commGrpRanksWorld.append(len(locations))
        locations.append(Location("127.0.0.1", port, i, None, None, args.cpp))
        port += 1
    addrToBindCombo = re.split('[-:]', args.addrToBind)
    addrToBind = addrToBindCombo[0]
    portToBind = int(addrToBindCombo[1])

    coordinator = ClusterCoordinator(addrToBind, portToBind, locations, os.getcwd(), args.be_batch_size)
    if args.install:
        coordinator.installPackages()

    # Just make sure there's no previously left runtimes.
    # CPP runtimes seem to terminate appropriately. So, there's no need to shutdown leftovers.
    if not args.cpp:
        print("Cleaning up potentially leftover runtime servers from previous experiment.")
        coordinator.shutdownRuntimeAll()
        time.sleep(10)

    coordinator.launchRuntimeAll(args.c10dBackend, profile=args.profile, cppRuntime=args.cpp, manualLaunch=args.manualLaunch)
    print("All runtime nodes are up and running. Now, initializing communication backend..")
    coordinator.initCommBackendAll(args.c10dBackend, commGrpRanksWorld)
    print("Communication backends are ready at all locations.")
    print("Now, cluster is ready to accept training jobs.")
    sys.stdout.flush()

    coordinator.timeout = 1
    while not HAS_EXCEPTION:
        coordinator.handle_request()

    time.sleep(5)

if __name__ == "__main__":
    main()
