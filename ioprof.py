#!/usr/bin/python -tt
# I/O Profiler for Linux
# Copyright (c) 2017, Intel Corporation.
#
# This program is free software; you can redistribute it and/or modify it
# under the terms and conditions of the GNU General Public License,
# version 2, as published by the Free Software Foundation.
#
# This program is distributed in the hope it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

import sys, getopt, os, re, string, stat, subprocess, math, shlex, time
from multiprocessing import Pool, Process, Lock, Manager, Value, Array
import multiprocessing
from argparse import ArgumentParser
import logging

# Global Variables
logger = None
log_format = "[%(levelname)s] %(message)s" # "%(asctime)s [%(levelname)s] %(message)s"

class global_variables:
    #VERBOSE   = False
    def __init__(self):
        self.version           = "1.0.0.1"                   # Version string
        self.verbose           = False                       # Verbose logging (-v flag)
        self.debug             = False                       # Debug log level (-x flag)
        self.single_threaded   = True                        # Single threaded for debug/profiling
        self.manager           = Manager()                   # Multiprocess sync object

        self.file_list         = []                          # File List

        self.io_total          = Value('L', 0)               # Number of total I/O's
        self.read_total        = Value('L', 0)               # Number of buckets read (1 I/O can touch many buckets)
        self.write_total       = Value('L', 0)               # Number of buckets written (1 I/O can touch many buckets)
        self.reads             = self.manager.dict()         # Array of read hits by bucket ID
        self.writes            = self.manager.dict()         # Array of write hits by bucket ID
        self.r_totals          = self.manager.dict()         # Hash of read I/O's with I/O size as key
        self.w_totals          = self.manager.dict()         # Hash of write I/O's with I/O size as key
        self.bucket_hits_total = Value('L', 0)               # Total number of bucket hits (not the total buckets)
        self.total_blocks      = Value('L', 0)               # Total number of LBA's accessed during profiling
        self.files_to_lbas     = self.manager.dict()         # Files and the lba ranges associated with them
        self.max_bucket_hits   = Value('L', 0)               # The hottest bucket
        self.bucket_to_files   = self.manager.dict()         # List of files that reside on each bucket
        self.term              = Value('L', 0)               # Thread pool done with work
        self.trace_files       = False                       # Map filesystem files to block LBAs

        ### Semaphores: These are the locks for the shared variables
        self.read_semaphore            = self.manager.Lock() # Lock for the global read hit array
        self.write_semaphore           = self.manager.Lock() # Lock for the global write hit array
        self.read_totals_semaphore     = self.manager.Lock() # Lock for the global read totals
        self.write_totals_semaphore    = self.manager.Lock() # Lock for the global write totals
        self.total_semaphore           = self.manager.Lock() # Lock for the global I/O totals
        self.total_blocks_semaphore    = self.manager.Lock() # Lock for the global total LBA's accessed
        self.files_to_lbas_semaphore   = self.manager.Lock() # Lock for the global file->lba mapping hash
        self.max_bucket_hits_semaphore = self.manager.Lock() # Lock for the global maximum hits per bucket
        self.bucket_to_files_semaphore1 = self.manager.Lock() # Lock for the global bucket_to_files
        self.bucket_to_files_semaphore2 = self.manager.Lock() # Lock for the global bucket_to_files
        self.term_semaphore            = self.manager.Lock() # Lock for the global TERM
        self.trace_files_semaphore     = self.manager.Lock() # Lock for the global trace_files
        self.file_hit_count_semaphore  = self.manager.Lock() # Lock for the global file_hit_count

        # Thread-local variables.  Use these to avoid locking constantly
        self.thread_io_total   = 0          # Thread-local total I/O count (I/O ops)
        self.thread_r_totals   = {}         # Thread-local read I/O size counts (ops)
        self.thread_w_totals   = {}         # Thread-local write I/O size counts (ops)
        self.thread_bucket_hits_total = 0   # Thread-local total bucket hits (buckets)
        self.thread_read_total = 0          # Thread-local total read count (I/O ops)
        self.thread_write_total = 0         # Thread-local total write count (I/O ops)
        self.thread_reads = {}              # Thread-local read count dict (buckets)
        self.thread_writes = {}             # Thread-local write count dict (buckets)
        self.thread_total_blocks = 0        # Thread-local total blocks accessed (lbas)
        self.thread_max_bucket_hits = 0     # Thread-local maximum bucket hits (bucket hits)

        # Globals
        self.file_hit_count    = {}         # Count of I/O's to each file
        self.cleanup           = []         # Files to delete after running this script
        self.total_lbas        = 0          # Total logical blocks, regardless of sector size
        self.tarfile           = ''         # .tar file outputted from 'trace' mode
        self.fdisk_file        = ""         # File capture of fdisk tool output

        self.top_files         = []         # Top files list
        self.device            = ''         # Device (e.g. /dev/sdb)
        self.device_str        = ''         # Device string (e.g. sdb for /dev/sdb)

        # Unit Scales
        self.KiB               = 1024       # 2^10
        self.MiB               = 1048576    # 2^20
        self.GiB               = 1073741824 # 2^30

        # Config settings
        self.bucket_size        = 1 * self.MiB # Size of the bucket for totaling I/O counts (e.g. 1MB buckets)
        self.num_buckets        = 1            # Number of total buckets for this device
        self.timeout            = 3            # Seconds between each print
        self.runtime            = 0            # Runtime for 'live' and 'trace' modes
        self.live_itterations   = 0            # How many iterations for live mode.  Each iteration is 'timeout' seconds long
        self.sector_size        = 0            # Sector size (usually obtained with fdisk)
        self.percent            = 0.020        # Histogram threshold for each level (e.g. 0.02% of total drive size)
        self.total_capacity_gib = 0            # Total drive capacity
        self.mode               = ''           # Processing mode (live, trace, post)
        self.pdf                = False        # Generate a PDF report instead of a text report
        self.top_count_limit    = 10           # How many files to list in Top Files list (e.g. Top 10 files)
        self.thread_count       = 0            # Thread Count
        self.cpu_affinity       = 0            # Tie each thread to a CPU for load balancing
        self.thread_max         = 32           # Max thread cout
        self.buffer_size        = 1024         # blktrace buffer size
        self.buffer_count       = 8            # blktrace buffer count

        # Gnuplot settings
        self.x_width            = 800          # gnuplot x-width
        self.y_height           = 600          # gnuplot y-height

        ### ANSI COLORS
        self.black   = "\e[40m"
        self.red     = "\e[41m"
        self.green   = "\e[42m"
        self.yellow  = "\e[43m"
        self.blue    = "\e[44m"
        self.magenta = "\e[45m"
        self.cyan    = "\e[46m"
        self.white   = "\e[47m"
        self.none    = "\e[0m"

        ### Heatmap Key
        self.colors = [self.black, self.red, self.green, self.yellow, self.blue, self.magenta, self.cyan, self.white, self.none]

        ### Heatmap Globals (TODO)
        self.color_index = 0
        self.choices = len(self.colors)
        self.vpc = 1
        self.cap = 0
        self.rate = 0

        self.mount_point        = ""
        self.extents            = []
        self.files              = []
# global_variables

### Print usage
def usage(g):
    name = os.path.basename(__file__)
    #name = argv[0]
    logger.info("Invalid command\n")
    #print name + " " + str(argv)
    print (name, end='')
    logger.info("\n\nUsage:")
    logger.info(name + " -m trace -d <dev> -r <runtime> [-v] [-f] # run trace for post-processing later")
    logger.info(name + " -m post  -t <dev.tar file>     [-v] [-p]   # post-process mode")
    logger.info(name + " -m live  -d <dev> -r <runtime> [-v]        # live mode")
    logger.info("\nCommand Line Arguments:")
    logger.info("-d <dev>            : The device to trace (e.g. /dev/sdb).  You can run traces to multiple devices (e.g. /dev/sda and /dev/sdb)")
    logger.info("                      at the same time, but please only run 1 trace to a single device (e.g. /dev/sdb) at a time")
    logger.info("-r <runtime>        : Runtime (seconds) for tracing")
    logger.info("-t <dev.tar file>   : A .tar file is created during the 'trace' phase.  Please use this file for the 'post' phase")
    logger.info("                      You can offload this file and run the 'post' phase on another system.")
    logger.info("-v                  : (OPTIONAL) Print verbose messages.")
    logger.info("-f                  : (OPTIONAL) Map all files on the device specified by -d <dev> during 'trace' phase to their LBA ranges.")
    logger.info("                       This is useful for determining the most fequently accessed files, but may take a while on really large filesystems")
    logger.info("-p                  : (OPTIONAL) Generate a .pdf output file in addition to STDOUT.  This requires 'pdflatex', 'gnuplot' and 'terminal png'")
    logger.info("                       to be installed.")
    sys.exit(-1)
# usage (DONE)

def set_globals(g, command_args):
    """
    Set global vars
    Arg(s):
        argv : arguments passed in via command line
    Return
        Parsed arguments and build request flag or exception otherwise
    """
    global logger

    g.mode = command_args.mode
    g.device = command_args.device
    g.tarfile = command_args.tarfile
    logger.info(command_args)
    g.trace_files = command_args.trace_files
    g.runtime = command_args.runtime
    if g.runtime is not None:
        g.runtime = int(g.runtime)
        if g.runtime < 3:
            g.runtime = 3 # Minimum runtime
    g.verbose = command_args.verbose
    g.pdf = command_args.pdf
    g.debug = command_args.debug
    if g.debug is True:
        logger.setLevel(logging.DEBUG)

    # Check arguments
    if g.verbose == True or g.debug == True:
        logger.warning( "verbose: " + str(g.verbose) + " debug: " + str(g.debug))

    if g.mode == 'live':
        logger.warning( "LIVE")
        if g.device == '' or g.runtime == '':
            usage(g)
        logger.debug( "Dev: " + g.device + " Runtime: " + g.runtime)
        match = re.search("\/dev\/(\S+)", g.device)
        try: 
            logger.debug(match.group(1))
            g.device_str = string.replace(match.group(1), "/", "_")
        except:
            logger.info("Invalid Device Type")
            usage(g)
        statinfo = os.stat(g.device)
        if not stat.S_ISBLK(statinfo.st_mode):
            logger.info("Device " + g.device + " is not a block device")
            usage(g)
    elif g.mode == 'post':
        logger.warning( "POST")
        if g.tarfile == '':
            usage(g)
        match = re.search("(\S+).tar", g.tarfile)
        try:
            logger.debug(match.group(1))
            g.device_str = match.group(1)
        except:
            logger.info("ERROR: invalid tar file" + g.tarfile)
        if g.pdf == True:
            logger.warning( "PDF Report Output")
            check_pdf_prereqs(g)

            logger.info("PDF Report Output - COMING SOON...") # COMING SOON
            sys.exit(-1) # COMING SOON
        g.fdisk_file = "fdisk." + g.device_str
        logger.debug( "fdisk_file: " + g.fdisk_file)
        g.cleanup.append(g.fdisk_file)
    elif g.mode == 'trace':
        logger.warning( "TRACE")
        check_trace_prereqs(g)
        if g.device == '' or g.runtime == '':
            usage(g)
        logger.debug( "Dev: " + g.device + " Runtime: " + str(g.runtime))
        match = re.search("\/dev\/(\S+)", g.device)
        try: 
            logger.debug(match.group(1))
            g.device_str = match.group(1)
            g.device_str = g.device_str.replace("/", "_")
        except BaseException as ex:
            logger.info(f"Invalid Device Type: {ex}")
            usage(g)
            sys.exit(1)
        statinfo = os.stat(g.device)
        logger.info(statinfo)
        if not stat.S_ISBLK(statinfo.st_mode):
            logger.info("Device " + g.device + " is not a block device")
            usage(g)
            sys.exit(1)
    else:
        usage(g)
    return

def get_arguments(argv=None):
    """
    Parse the script command line arguments
    Arg(s):
        argv : arguments passed in via command line
    Return
        Parsed arguments and build request flag or exception otherwise
    """
    if argv is None:
        argv = sys.argv
    else:
        sys.argv.extend(argv)

    try:
        parser = ArgumentParser()

        # Full path log file name
        parser.add_argument("-m", "--mode", type=str, help="Mode (trace, post, live)")
        parser.add_argument("-d", "--device", type=str, help="Device to trace, (i.e. -d /dev/nvme0n1)")
        parser.add_argument("-t", "--tarfile", type=str, help="Tarfile, output from -m trace")
        parser.add_argument("-r", "--runtime", type=str, help="Runtime in seconds")
        parser.add_argument("--trace_files", "--f",  action='store_true', default=False, help='Trace Files')
        parser.add_argument("--verbose", "--v", action='store_true',default=False, help='Print verbose')
        parser.add_argument( "--pdf", "--p", action='store_true',default=False, help='Output PDF')
        parser.add_argument("--debug", "--x", action='store_true',default=False, help='Debug mode')
        
        # Process arguments
        return parser.parse_args()

    except BaseException as base_ex:
        if str(base_ex) != "0":
            sys.stderr.write('For help use --help\n\n')
            logger.info(base_ex)
            sys.exit(1)
        else:
            logger.info(base_ex)
            sys.exit(1)
        return None

### Check prereqs for gnuplot and latex
def check_pdf_prereqs(g):
    logger.debug( "check_pdf_prereqs")

    rc = os.system("which gnuplot &> /dev/null")
    if rc != 0:
        logger.info("ERROR: gnuplot not installed.  Please offload the trace file for processing.")
        sys.exit(1)
    else:
        logger.debug( "which gnuplot: rc=" + str(rc))
    rc = os.system("which pdflatex &> /dev/null")
    if rc != 0:
        logger.info("ERROR: pdflatex not installed.  Please offload the trace file for processing.")
        sys.exit(1)
    else:
        logger.debug( "which pdflatex: rc=" + str(rc))
    rc = os.system("echo 'set terminal png' > pngtest.txt; gnuplot pngtest.txt >/dev/null 2>&1")
    if rc != 0:
        logger.info("ERROR: gnuplot PNG terminal not installed.  Please offload the trace file for processing.")
        sys.exit(1)
    else:
        logger.debug( "gnuplot pngtest.txt: rc=" + str(rc))
    return
# check_pdf_prereqs (DONE)

### Check prereqs for blktrace
def check_trace_prereqs(g):
    logger.debug( "check_trace_prereqs")
    rc = os.system("which blktrace 1>/dev/null 2>/dev/null")
    if rc != 0:
        logger.info("ERROR: blktrace not installed.  Please install blktrace")
        sys.exit(1)
    else:
        logger.debug( "which blktrace: rc=" + str(rc))
    rc = os.system("which blkparse 1>/dev/null 2>/dev/null")
    if rc != 0:
        logger.info("ERROR: blkparse not installed.  Please install blkparse")
        sys.exit(1)
    else:
        logger.debug( "which blkparse: rc=" + str(rc))
# check_trace_prereqs (DONE)

### Check if debugfs is mounted
def mount_debugfs(g):
    rc = os.system("mount | grep debugfs 1>/dev/null 2>/dev/null")
    if rc != 0:
        logger.debug( "Need to mount debugfs")
        rc = os.system("mount -t debugfs debugfs /sys/kernel/debug")
        if rc != 0:
            logger.info("ERROR: Failed to mount debugfs")
            sys.exit(2)
        else:
            logger.warning( "Mounted debugfs successfully")
    return
# mount_debugfs (DONE)

### Translate LBA to Bucket
def lba_to_bucket(g, lba):
    bucket = (int(lba) * int(g.sector_size)) / int(g.bucket_size)
    if bucket > g.num_buckets:
        #printf("ERROR: lba=%d bucket=%d greater than num_buckets=%d\n", int(lba), bucket, g.num_buckets)
        bucket = g.num_buckets - 1
    return bucket
# lba_to_bucket (DONE)

### Translate Bucket to LBA
def bucket_to_lba(g, bucket):
    lba = (bucket * g.bucket_size) / g.sector_size
    return lba
# bucket_to_lba (DONE)

### debugfs method
### # This method can only be used on ext2/ext3/ext4 filesystems
### I don't plan on using this method, long term
### In testing the debugfs method, I found it to be approximately 30% slower than the ioctl method in perl
def debugfs_method(g, file):
    extents = []
    file = string.replace(file, g.mountpoint, "")
    logger.debug( "file: " + file)
    cmd = 'debugfs -R "dump_extents ' + file + '" ' + g.device + '  2>/dev/null'
    logger.debug( cmd)
    (rc, extent_out) = run_cmd(g, cmd)
    for line in extent_out:
        logger.debug( line)
        match = re.search("\s+\d+\/\s+\d+\s+\d+\/\s+\d+\s+\d+\s+-\s+\d+\s+(\d+)\s+-\s+(\d+)", line)
        try:
            g.extents.append(match.group(1) + ":" + match.group(1))
        except:
            logger.debug( "no match")
    return
# debugfs_method (DONE)

### Translate FS cluster to LBA
def fs_cluster_to_lba(g, fs_cluster_size, sector_size, io_cluster):
    lba = io_cluster * (fs_cluster_size / sector_size)
    return lba
# fs_cluster_to_lba (DONE)

### ioctl method (COMING SOON...)
### # This method "should" be usable regardless of filesystem
### There is some risk that FIBMAP!=1.  Need to address this later
### I plan to use the ioctl method because it is 30% faster than the debugfs method
def ioctl_method(g, file):
    logger.info("ext3 will be supported once ioctl_method() is supported")
    sys.exit(-1)
    return
# ioctl_method (COMING SOON...)

### Print filetrace files
def printout(g, file):
    logger.debug( "printout: " + file)
    cpu_affinity = 0
    filetrace = "filetrace." + g.device_str + "." + str(cpu_affinity) + ".txt"
    fo = open(filetrace, "a")
    try:
        fo.write(file + " :: " + g.extents)
        fo.close()
    except:
        logger.info("ERROR: Failed to open " + filetrace)
        sys.exit(3)
# printout (DONE)

def block_ranges(g, file):
    logger.debug( "block_ranges: " + file)
    # TODO: exclude /proc and /sys mountpoints
    statinfo = os.stat(file)
    mode = statinfo.st_mode
    if stat.S_ISLNK(mode) or stat.ST_SIZE == 0 or not stat.ST_ISREG(mode):
        logger.debug( "Disqualified file: " + file)
        return
    mounttype = os.system("mount | grep " + g.device + " | awk '{ print \$5 }'")
    if mounttype == "ext4":
        debugfs_method(g, file)
    elif mounttype == "ext3":
        ioctl_method(g, file)
    else:
        logger.info("ERROR: " + mounttype + " is not supported yet")
        sys.exit(4)
    printout(file)
    return
# block_ranges (DONE)

def find_all_files(g):
    files = []
    logger.info("FIND ALL FILES")
    os.system("rm -f filetrace.* &>/dev/null")
    cpu_affinity = 0
    filetrace = "filetrace." + g.device_str + "." + str(cpu_affinity) + ".txt"
    os.system("touch " + filetrace)
    cmd = "cat /proc/cpuinfo | grep processor | wc -l"
    (rc, cpu_count) = run_cmd(g, cmd)

    cmd = "mount | grep " +  g.device + "| awk '{ print \$3 }'"
    (rc, mountpoint) = run_cmd(g, cmd)
    if rc != 0:
        logger.info(g.device + " not mounted")
        os.system("gzip --fast filetrace.* &>/dev/null")
        return
    logger.warning( "mountpoint: " + mountpoint)

    cmd = 'mount | grep ' + g.device + " | awk '{ print \$5 }'"
    (rc, mounttype)  = run_cmd(g, cmd)
    logger.warning( "mounttype: " + mounttype)

    ioprof_file = 'ioprof_files.' + g.device_str + '.txt'
    cmd = 'find ' + mountpoint + ' -xdev -name "*" > ' + ioprof_file
    rc = os.system(cmd)
    with open(ioprof_file, "r") as fo:
        for line in fo:
            g.files.append(line)
            logger.debug( line)
    fo.close()

    file_count = len(g.files)
    logger.debug( "filecount: " + file_count)

    # Single Threaded Method (TODO: Make Multi-threaded)
    k=0
    for i in range(0..file_count):
        if (k > 100):
            progress = (i / file_count) * 100
            printf("\r%05.2f%% COMPLETE", progress)
            k=0
        k = k + 1
        file = g.files[i]
        logger.debug("file: " + file)
        block_ranges(file)
        
    os.system("gzip --fast filetrace.* &>/dev/null")
    return
# find_all_files (DONE)

### Translate a bucket ID to a list of files
def bucket_to_file_list(g, bucket_id):
    list=""
    try:
         list = g.bucket_to_files[bucket_id]
    except:
        pass
    return list
# bucket_to_file_list (DONE)

def file_to_bucket_helper(g, f):
    for file, r in f.items():
        #g.file_hit_count_semaphore.acquire()
        #g.file_hit_count[file]=0 # Initialize file hit count
        #g.file_hit_count_semaphore.release()
        tempstr = f[file]
        logger.debug( "f=" + file + " r=" + r)
        x=0
        for range in tempstr.split(' '):
            if range == ' ' or range == '':
                continue # TODO
            logger.debug( 'r(' + str(x) + ')=' + range)
            x+=1
            try:
                (start, finish) = range.split(':')
            except:
                continue
            logger.debug( file + " start=" + start + ", finish=" + finish)
            if start == '' or finish == '':
                continue
            start_bucket  = lba_to_bucket(g, start)
            finish_bucket = lba_to_bucket(g, finish)
    
            logger.debug( file + " s_lba=" + start + " f_lba=" + finish + " s_buc=" + str(start_bucket) + "f_buc=" + str(finish_bucket ))
            #print "WAITING ON LOCK"
            i=start_bucket
            #print "GOT LOCK!"
            while i<= finish_bucket:
                if (i < (g.num_buckets/2)):
                    g.bucket_to_files_semaphore1.acquire()
                else:
                    g.bucket_to_files_semaphore2.acquire()
                try:
                    g.bucket_to_files[i] = g.bucket_to_files[i] + file + " "
                except:
                    g.bucket_to_files[i] = file + " "
                if (i < (g.num_buckets/2)):
                    g.bucket_to_files_semaphore1.release()
                else:
                    g.bucket_to_files_semaphore2.release()
                i+=1
                continue

                logger.debug( "i=" + str(i))
                if i in g.bucket_to_files:
                    pattern = re.escape(file)
                    match = re.search(pattern, g.bucket_to_files[i])
                    try:
                        match.group(0)
                    except:
                        logger.debug( "No Match!  FILE=" + pattern + " PATTERN=" + g.bucket_to_files[i])
                        g.bucket_to_files[i] = g.bucket_to_files[i] + file + " "
                else:
                    g.bucket_to_files[i] = file + " "
                logger.debug( "i=" + str(i) + "file_to_buckets: " + g.bucket_to_files[i])
                i+=1
    return

### Tranlate a file to a list of buckets
def file_to_buckets(g):
    k=0
    size = len(g.files_to_lbas)
    logger.info(f"files_to_lbas={size}")
    logger.info("Moving some memory around.  This will take a few seconds...")
    f = dict(g.files_to_lbas)
    temp = {}
    plist = []
    g.thread_max = 1024

    #if g.single_threaded == False:
    if False:
        for file, r in f.items():
            g.file_hit_count[file]=0 # Initialize file hit count
            temp[file] = r
            k+=1
            if k % 10 == 0:
                p = Process(target=file_to_bucket_helper, args=(g, temp))
                plist.append(p)
                p.start()
                printf("\rfile_to_buckets: %d %% (%d of %d)", (k*100 / size), k, size)
                sys.stdout.flush()
            #if False:
            while len(plist) > g.thread_max:
                for p in plist:
                    try:
                        p.join(0)
                    except:
                        pass
                    else:
                        if not p.is_alive():
                            plist.remove(p)
                    time.sleep(0.10)
        x=1
        while len(plist) > 0:
            dots=""
            for i in xrange(x):
                dots = dots + "."
            x+=1
            if x>3:
                x=1
            printf("\rWaiting on %3d threads to complete processing%-3s", len(plist), dots)
            printf("    ")
            sys.stdout.flush()
            for p in plist:
                try:
                    p.join(0)
                except:
                    pass
                else:
                    if not p.is_alive():
                        plist.remove(p)
            time.sleep(0.10)
    
        logger.info("\rDone correlating files to buckets.  Now time to count bucket hits")
        return
    else:
        logger.debug("HERE!")
        for file, r in f.items():
            k+=1
            if k % 100 == 0:
                printf("\rfile_to_buckets: %d %% (%d of %d)", (k*100 / size), k, size)
                sys.stdout.flush()
            g.file_hit_count[file]=0 # Initialize file hit count
            tempstr = f[file]
            logger.debug( "f=" + file + " r=" + r)
            x=0
            for range in tempstr.split(' '):
                if range == ' ' or range == '':
                    continue # TODO
                logger.debug( 'r(' + str(x) + ')=' + range)
                x+=1
                try:
                    (start, finish) = range.split(':')
                except:
                    continue
                logger.debug( file + " start=" + start + ", finish=" + finish)
                if start == '' or finish == '':
                    continue
                start_bucket  = lba_to_bucket(g, start)
                finish_bucket = lba_to_bucket(g, finish)
    
                logger.debug( file + " s_lba=" + start + " f_lba=" + finish + " s_buc=" + str(start_bucket) + "f_buc=" + str(finish_bucket ))
                i=start_bucket
                while i<= finish_bucket:
                    logger.debug( "i=" + str(i))
                    if i in g.bucket_to_files:
                        pattern = re.escape(file)
                        match = re.search(pattern, g.bucket_to_files[i])
                        try:
                            match.group(0)
                        except:
                            logger.debug( "No Match!  FILE=" + pattern + " PATTERN=" + g.bucket_to_files[i])
                            g.bucket_to_files[i] = g.bucket_to_files[i] + file + " "
                    else:
                        g.bucket_to_files[i] = file + " "
                    logger.debug( "i=" + str(i) + "file_to_buckets: " + g.bucket_to_files[i])
                    i+=1
        logger.info("\rDone correlating files to buckets.  Now time to count bucket hits")
        return
# file_to_buckets (DONE)

### Add up I/O hits to each file touched by a bucket
def add_file_hits(g, bucket_id, io_count):
    list = bucket_to_file_list(g, bucket_id)
    size = len(list)
    #print list
    if size == 0 and io_count != 0:
        logger.debug( "No file hit.  bucket=" + str(bucket_id) + ", io_cnt=" + str(io_count))

    for file in list.split(' '):
        if file != '': 
            logger.debug( "file=" + file)
            try:
                g.file_hit_count[file] += io_count
            except:
                g.file_hit_count[file] = io_count
    return
# add_file_hits (DONE)

### Get logrithmic theta for Zipfian distribution
def theta_log(g, base, value):
    logger.debug( "base=" + str(base) + ", value=" + str(value))
    if value == 0 or base == 0:
        return 0
    else:
        result = math.log(value) / math.log(base)
        return result
# theta_log (DONE)

### Print Results
def print_results(g):
    num=0
    sum=0
    k=0
    buffer=''
    row=0
    column=0
    bw_total=0
    counts={}
    read_sum=0
    write_sum=0
    row=column=0
    bw_total=0
    histogram_iops=[]
    histogram_bw=[]
    
    g.verbose=True
    logger.warning( "num_buckets=" + str(g.num_buckets) + " bucket_size=" + str(g.bucket_size))
    
    g.verbose=False
    if g.pdf == True:
        # TODO
        pass
    x=0
    threshold = g.num_buckets / 100
    i=0
    while i<g.num_buckets:
        x+=1
        if x > threshold:
            printf("\rBucket Percent: %d %% (%d of %d)", ((i * 100)/ g.num_buckets), i, g.num_buckets)
            sys.stdout.flush()
            x=0
        if i != 0 and (i % g.x_width) == 0:
            if g.pdf == True:
                # TODO
                pass
            buffer=''
            row += 1
            column=0
        

        r=0
        if i in g.reads:
            r=g.reads[i]
        w=0
        if i in g.writes:
            w=g.writes[i]

        bucket_total = r + w
        bw_total += bucket_total * g.bucket_size
        if g.trace_files:
            add_file_hits(g, i, bucket_total)
        if bucket_total in counts:
            counts[bucket_total] += 1
        else:
            counts[bucket_total] = 1
        #logger.debug( "bucket_total=" + str(bucket_total) + " counts[b_bucket_total]=" + str(counts[bucket_total]))
        read_sum += r
        write_sum += w
        buffer = buffer + ("%d " %  bucket_total)
        column += 1
        i+=1

    logger.info("\r                             ")
    while (i % g.x_width) != 0:
        i+=1
        buffer = buffer + "0 "

    if g.pdf:
        # TODO
        pass

    logger.warning( "num_buckets=%s pfgp iot=%s bht=%s r_sum=%s w_sum=%s yheight=%s" % (g.num_buckets, g.io_total.value, g.bucket_hits_total.value, read_sum, write_sum, g.y_height))

    t=0
    j=0
    section_count=0
    b_count=0
    gb_tot = 0
    bw_tot = 0
    bw_count = 0
    io_sum = 0
    tot = 0
    if g.pdf:
        # TODO
        pass

    max_set = 0
    max = 0
    theta_count = 1
    theta_total = 0
    min = 1
    max_theta = 0
    min_theta = 999


    # %counts is a hash
    # each key "bucket_total" represents a particular I/O count for a bucket
    # each value represents the number of buckets that had this I/O count
    # This allows me to quickly tally up a histogram and is pretty
    # space efficient since most buckets tend to have zero I/O that
    # key tends to have the largest number of buckets
    #
    # Iterate through each key in decending order
    for total in sorted(counts, reverse=True):
        logger.debug( "total=" + str(total) + " counts=" + str(counts[total]))
        if total > 0:
            tot += total * counts[total]
            if max_set == 0:
                max_set=1
                max = total
            else:
                theta_count += 1
                min = total
                cur_theta = theta_log(g, theta_count, max) - theta_log(g, theta_count, total)
                if cur_theta > max_theta:
                    max_theta = cur_theta
                if cur_theta < min_theta:
                    min_theta = cur_theta
                logger.debug( "cur_theta=" + str(cur_theta))
                theta_total += cur_theta
            i=0
            while i<counts[total]:
                section_count += total
                b_count += 1
                bw_count += total * g.bucket_size
                if ((b_count * g.bucket_size )/ g.GiB) > (g.percent * g.total_capacity_gib):
                    logger.debug( "b_count:" + str(b_count))
                    bw_tot += bw_count
                    gb_tot += (b_count * g.bucket_size)
                    io_sum += section_count
    
                    gb = "%.1f" % (gb_tot / g.GiB)
                    if g.bucket_hits_total.value == 0:
                        io_perc = "NA"
                        io_sum_perc = "NA"
                        bw_perc = "NA"
                    else:
                        logger.debug( "b_count=" + str(b_count) + " s=" + str(section_count) + " ios=" + str(io_sum) + " bwc=" + str(bw_count))
                        io_perc = "%.1f" % ((float(section_count) / float(g.bucket_hits_total.value)) * 100.0)
                        io_sum_perc = "%.1f" % ((float(io_sum) / float(g.bucket_hits_total.value)) * 100.0)
                        if bw_total == 0:
                            bw_perc = "%.1f" % (0)
                        else:
                            bw_perc = "%.1f" % ((bw_count / bw_total) * 100)
    
                    if g.pdf:
                        # TODO
                        pass
                    
                    histogram_iops.append(str(gb) + " GB " + str(io_perc) + "% (" + io_sum_perc + "% cumulative)")
                    histogram_bw.append(str(gb) + " GB " + str(bw_perc) + "% ")

                    b_count=0
                    section_count=0
                    bw_count=0
                    
                i += 1
    if b_count:
        logger.debug( "b_count: " + str(b_count))
        bw_tot += bw_count
        gb_tot += b_count * g.bucket_size
        io_sum += section_count

        gb = "%.1f" % (gb_tot / g.GiB)
        if g.bucket_hits_total.value == 0:
            io_perc = "NA"
            io_sum_perc = "NA"
            bw_perc = "NA"
        else:
            io_perc = "%.1f" % ((section_count / g.bucket_hits_total.value) * 100)
            io_sum_perc = "%.1f" % ((io_sum / g.bucket_hits_total.value) * 100)
            if bw_total == 0:
                bw_perc = "%.1f" % (0)
            else:
                bw_perc = "%.1f" % ((bw_count / bw_total) * 100)

        if g.pdf:
            # TODO
            pass

        histogram_iops.append(str(gb) + " GB " + str(io_perc) + "% (" + str(io_sum_perc) + "% cumulative)")
        histogram_bw.append(str(gb) + " GB " + str(bw_perc) + "% ")

        b_count = 0
        section_count = 0
        bw_count = 0

    if g.pdf:
        # TODO
        pass

    logger.debug( "t=" + str(t))

    logger.info("--------------------------------------------")
    logger.info("Histogram IOPS:")
    for entry in histogram_iops:
        logger.info(entry)
    logger.info("--------------------------------------------")

    # TODO: Check that this is consistent with Perl version
    if (theta_count):
        avg_theta = theta_total / theta_count
        med_theta = ((max_theta - min_theta) / 2 ) + min_theta
        approx_theta = (avg_theta + med_theta) / 2
        #string = "avg_t=%s med_t=%s approx_t=%s min_t=%s max_t=%s\n" % (avg_theta, med_theta, approx_theta, min_theta, max_theta)
        logger.warning( "avg_t=%s med_t=%s approx_t=%s min_t=%s max_t=%s\n" % (avg_theta, med_theta, approx_theta, min_theta, max_theta))
        analysis_histogram_iops = "Approximate Zipfian Theta Range: %0.4f-%0.4f (est. %0.4f).\n" % (min_theta, max_theta, approx_theta)
        logger.info(analysis_histogram_iops)

    logger.debug( "Trace_files: " + str(g.trace_files))
    if g.trace_files:
        top_count=0
        logger.info("--------------------------------------------")
        logger.info("Top files by IOPS:")
        logger.info("Total I/O's: " + str(g.bucket_hits_total.value))
        if g.bucket_hits_total.value == 0:
            logger.info("No Bucket Hits")
        else:    
            for filename in sorted(g.file_hit_count, reverse=True, key=g.file_hit_count.get):
                hits = g.file_hit_count[filename]
                if hits > 0:
                    hit_rate = (float(hits) / float(g.bucket_hits_total.value)) * 100.0
                    logger.info("%0.2f%% (%d) %s" % (hit_rate, hits, filename))
                    if g.pdf:
                        g.top_files.append("%0.2f%%: (%d) %s\n" % (hit_rate, hits, filename))
                top_count += 1
                if top_count > g.top_count_limit:
                    break
        logger.info("--------------------------------------------")
    
    return
# print_results (IN PROGRESS)

### Print heatmap header for PDF
def print_header_heatmap(g):
    return
# print_header_heatmap (TODO)

### Print histogram header for PDF
def print_header_histogram_iops(g):
    return
# print_header_histogram_iops (TODO)

### Print stats header for PDF
def print_header_stats_iops(g):
    return
# print_header_stats_iops (TODO)

### Create PDF Report
def create_report(g):
    return
# create_report (TODO)

### Print I/O statistics
def print_stats(g):
    return
# print_stats (TODO)


### Combine thread-local counts into global counts
def total_thread_counts (g, num):
    logger.debug("total_thread_counts total_thread_counts total_thread_counts total_thread_counts")

    g.max_bucket_hits_semaphore.acquire()
    logger.debug( "Thread " + str(num) + " has max_bucket_hits lock t=" + str(g.thread_max_bucket_hits) + " g=" + str(g.max_bucket_hits.value))
    if(g.thread_max_bucket_hits > g.max_bucket_hits.value):
        g.max_bucket_hits.value = g.thread_max_bucket_hits
    logger.debug( "Thread " + str(num) + " releasing max_bucket_hits lock t=" + str(g.thread_max_bucket_hits) + " g=" + str(g.max_bucket_hits.value))
    g.max_bucket_hits_semaphore.release()

    g.total_blocks_semaphore.acquire()
    logger.debug( "Thread " + str(num) + " has total_blocks lock t=" + str(g.thread_total_blocks) + " g=" + str(g.total_blocks.value))
    g.total_blocks.value += g.thread_total_blocks
    logger.debug( "Thread " + str(num) + " releasing total_blocks lock t=" + str(g.thread_total_blocks) + " g=" + str(g.total_blocks.value))
    g.total_blocks_semaphore.release()

    g.total_semaphore.acquire()
    logger.debug( "Thread " + str(num) + " has total lock t=" + str(g.thread_io_total) + " g=" + str(g.io_total.value))
    g.io_total.value += g.thread_io_total
    logger.debug( "Thread " + str(num) + " releasing total lock t=" + str(g.thread_io_total) + " g=" + str(g.io_total.value))
    g.total_semaphore.release()

    g.read_totals_semaphore.acquire()
    logger.debug( "Thread " + str(num) + " has read_totals lock t=" + str(g.thread_read_total) + " g=" + str(g.read_total.value))
    g.read_total.value += g.thread_read_total
    for io_size, hits in g.thread_r_totals.items():
        if io_size in g.r_totals:
            g.r_totals[io_size] += hits
        else:
            g.r_totals[io_size] = hits
    logger.debug( "Thread " + str(num) + " releasing read_totals lock t=" + str(g.thread_read_total) + " g=" + str(g.read_total.value))
    g.read_totals_semaphore.release()

    g.write_totals_semaphore.acquire()
    logger.debug( "Thread " + str(num) + " has write_totals lock t=" + str(g.thread_write_total) + " g=" + str(g.write_total.value))
    g.write_total.value += g.thread_write_total
    for io_size, hits in g.thread_w_totals.items():
        if io_size in g.w_totals:
            g.w_totals[io_size] += hits
        else:
            g.w_totals[io_size] = hits
    logger.debug( "Thread " + str(num) + " releasing write_totals lock t=" + str(g.thread_write_total) + " g=" + str(g.write_total.value))
    g.write_totals_semaphore.release()

    g.read_semaphore.acquire()
    logger.debug( "Thread " + str(num) + " has read lock.")
    for bucket,value in g.thread_reads.items():
        print(f"bucket={bucket}")
        try:
            g.reads[bucket] += value
        except:
            g.reads[bucket] = value
        logger.debug( "Thread " + str(num) + " has read lock.  Bucket=" + str(bucket) + " Value=" + str(value) + " g.reads[bucket]=" + str(g.reads[bucket]))
    g.read_semaphore.release()

    g.write_semaphore.acquire()
    logger.debug( "Thread " + str(num) + " has write lock.")
    for bucket,value in g.thread_writes.items():
        try:
            g.writes[bucket] += value
        except:
            g.writes[bucket] = value
        logger.debug( "Thread " + str(num) + " has write lock.  Bucket=" + str(bucket) + " Value=" + str(value) + " g.writes[bucket]=" + str(g.writes[bucket]))
    g.write_semaphore.release()

    g.total_semaphore.acquire()
    logger.debug( "Thread " + str(num) + " has total lock t=" + str(g.thread_bucket_hits_total) + " g=" + str(g.bucket_hits_total.value))
    g.bucket_hits_total.value += g.thread_bucket_hits_total
    logger.debug( "Thread " + str(num) + " releasing total lock t=" + str(g.thread_bucket_hits_total) + " g=" + str(g.bucket_hits_total.value))
    g.total_semaphore.release()

    return
# total_thread_counts (DONE)

### Thread parse routine for blktrace output
def thread_parse(g, file, num):
    logger.debug("thread_parse")
    logger.debug("========================")
    linecount = 0
    os.system("gunzip " + file + ".gz")
    logger.debug( "\nSTART: " +  file + " " + str(num) + "\n")
    try:
        with open(file, "r") as fo:

            count=0
            hit_count = 0
            for line in fo:
                count += 1
                result_set = regex_find(g, '(\S+)\s+Q\s+(\S+)\s+(\S+)$', line)
                if result_set != False:
                    #(a, b, c) = regex_find('.*Q.*', line)
                    hit_count += 1
                    #print len(set)
                    #print "a=" + a + " b=" + b + " c=" + c + "\n"
                    #print set
                    #sys.stdout.flush()
                    try:
                        #logger.debug("HIT HIT HIT")
                        parse_me(g, result_set[0], int(result_set[1]), int(result_set[2]))
                    except:
                        pass
                #sys.stdout.flush()

        total_thread_counts(g, num)
        logger.debug(  "\n FINISH" + file +  " (" + str(count) + " lines) [hit_count=" + str(hit_count) + "]" + str(g.thread_io_total) + "\n")
        rc = os.system("rm -f " + file)
    except BaseException as base_e:
        logger.error(f"ERROR: Failed to open {file}: {base_e}")
        sys.exit(3)
    return g

# thread_parse (DONE)

### Parse blktrace output
def parse_me(g, rw, lba, size):
    #logger.debug(  "rw=" + rw + " lba=" + str(lba) + " size=" + str(size))
    if (rw == 'R') or (rw == 'RW'):
        # Read
        #logger.debug("read")
        g.thread_total_blocks += int(size)
        g.thread_io_total += 1
        g.thread_read_total += 1
        if size in g.thread_r_totals:
            g.thread_r_totals[size] += 1
        else:
            g.thread_r_totals[size] = 1
        bucket_hits = (size * g.sector_size) / g.bucket_size
        bucket_hits = 1 # BEN
        if ((size * g.sector_size) % g.bucket_size) != 0:
            bucket_hits += 1
        for i in range(0, bucket_hits):
            bucket = int((lba * g.sector_size) / g.bucket_size) + i
            if bucket > g.num_buckets:
                # Not sure why, but we occassionally get buckets beyond our max LBA range
                bucket = g.num_buckets - 1
            if True:
                if bucket in g.thread_reads:
                    g.thread_reads[bucket] += 1
                else:
                    g.thread_reads[bucket] = 1
            else:
                try:
                    g.thread_reads[bucket] += 1
                except:
                    g.thread_reads[bucket] = 1
            if(g.thread_reads[bucket] > g.thread_max_bucket_hits):
                g.thread_max_bucket_hits = g.thread_reads[bucket]
            g.thread_bucket_hits_total += 1
    elif (rw == 'W') or (rw == 'WS'):
        # Write
        #logger.debug("write")
        g.thread_total_blocks += int(size)
        g.thread_io_total += 1
        g.thread_write_total += 1
        if size in g.thread_w_totals:
            g.thread_w_totals[size] += 1
        else:
            g.thread_w_totals[size] = 1
        bucket_hits = (size * g.sector_size) / g.bucket_size
        bucket_hits = 1 # BEN
        #print(bucket_hits)
        if ((size * g.sector_size) % g.bucket_size) != 0:
            bucket_hits += 1
        for i in range(0, bucket_hits):
            bucket = int((lba * g.sector_size) / g.bucket_size) + i
            if bucket > g.num_buckets:
                # Not sure why, but we occassionally get buckets beyond our max LBA range
                bucket = g.num_buckets - 1
            if True:
                if bucket in g.thread_writes:
                    g.thread_writes[bucket] += 1
                else:
                    g.thread_writes[bucket] = 1
            else:
                try:
                    g.thread_writes[bucket] += 1
                except:
                    g.thread_writes[bucket] = 1
            if(g.thread_writes[bucket] > g.thread_max_bucket_hits):
                g.thread_max_bucket_hits = g.thread_writes[bucket]
            g.thread_bucket_hits_total += 1
    return
# parse_me (DONE)

## File trace routine
def parse_filetrace(g, filename, num):
    print("PARSE_FILETRACE") # BEN
    thread_files_to_lbas = {}
    os.system("gunzip " + filename + ".gz")
    logger.debug( "tracefile = " + filename + " " + str(num) + "\n")
    try:
        fo = open(filename, "r")
    except Exception as e:
        logger.info("ERROR: Failed to open " + filename + " Err: ", e)
        sys.exit(3)
    else:
        for line in fo:
            result_set = regex_find(g, '(\S+)\s+::\s+(.+)', line)
            if result_set != False:
                object = result_set[0]
                ranges = result_set[1]
                thread_files_to_lbas[object] = ranges
                logger.debug( filename + ": obj=" + object + " ranges:" + ranges + "\n")
        fo.close()

        logger.debug( "Thread " + str(num) + "wants file_to_lba lock for " + filename + "\n")
        g.files_to_lbas_semaphore.acquire()
        for key,value in thread_files_to_lbas.items():
            g.files_to_lbas[key] = value
            logger.debug( "k=" + str(key) + " value=" + str(g.files_to_lbas[key]))
        g.files_to_lbas_semaphore.release()
        logger.debug( "Thread " + str(num) + "freed file_to_lba lock for " + filename + "\n")

    return
# parse_filetrace (DONE)

### Choose color for heatmap block
def choose_color(g, num):
    if num == -1 or num == 0:
        return g.black
    g.color_index = num / g.vpc
    if (g.color_index > (g.choices - 1)):
        g.debug = True
        logger.debug( "HIT! num=" + num)
        g.debug = False
        g.color_index=7
        return g.red
    color = g.colors[g.color_index]
    logger.debug( "cap=" + g.cap + " num=" + num + " ci=" + g.color_index + " vpc=" + g.vpc + " cap=" + g.cap)
    return color
# choose_color (DONE)

### Clear Screen for heatmap (UNUSED)
def clear_screen(g):
    logger.info("\033[2J")
    logger.info("\[\033[0;0f\]\r")
    return
# clear_screen (DONE)

### Get block value by combining buckets into larger heatmap blocks for term
def get_value(g, offset, rate):
    start = offset * rate
    end = start + rate
    sum = 0

    g.debug = True
    logger.debug( "start=" + start + " end=" + end)
    g.debug = False

    index=start
    while(index <= end):
        index+=1
        r = 0
        w = 0
        if index in g.reads:
            r = g.reads[index]
        if index in g.writes:
            w = g.writes[index]
        sum = sum + r + w

    g.debug = True
    logger.debug( "s=" + sum)
    g.debug = False

    return sum
# get_value (DONE)

def input_tar_files(g):
    cmd = 'tar -tf ' + g.tarfile 
    logger.debug(g.tarfile)
    (rc, file_text) = run_cmd(g, cmd)
    file_text = file_text.decode("utf-8")
    logger.debug( file_text)
    
    g.file_list = []
    for i in file_text.split("\n"):
        logger.debug( "i=" + i)
        if i != "":
            g.file_list.append(i)
    if rc != 0:
        logger.info("ERROR: Failed to test input file: " + g.tarfile)
        sys.exit(9)

    logger.info("Unpacking " + g.tarfile + ".  This may take a minute")
    cmd = 'tar -xvf ' + g.tarfile
    (rc, out) = run_cmd(g, cmd)
    if rc != 0:
        logger.info("ERROR: Failed to unpack input file: " + g.tarfile)
        sys.exit(9)
    else:
        logger.debug("Untar completed successfully")

    # Get fdisk info
    rc=0
    out=""
    (rc, out) = run_cmd(g, 'cat '+ g.fdisk_file )
    out = out.decode("utf-8")
    logger.info(out)
    result = regex_find(g, "Units = sectors of \d+ \S \d+ = (\d+) bytes", out)
    if result == False:
        #Units: sectors of 1 * 512 = 512 bytes
        result = regex_find(g, "Units: sectors of \d+ \* \d+ = (\d+) bytes", out)
        if result == False:
            logger.error("ERROR: Sector Size Invalid")
            logger.error(out)
            sys.exit()
    g.sector_size = int(result[0])
    logger.warning( "sector size="+ str(g.sector_size))
    result = regex_find(g, ".+ total (\d+) sectors", out)
    if result == False:
        #Disk /dev/sdb: 111.8 GiB, 120034123776 bytes, 234441648 sectors
        result = regex_find(g, "Disk /dev/\w+: \d+.\d+ [GT]iB, \d+ bytes, (\d+) sectors", out)
        if result == False:
            logger.info("ERROR: Total LBAs is Invalid")
            sys.exit()
    g.total_lbas  = int(result[0])
    logger.warning( "sector count ="+ str(g.total_lbas))

    result = regex_find(g, "Disk (\S+): \S+ GB, \d+ bytes", out)
    if result == False:
        # LINE:  Disk /dev/sdb: 111.8 GiB, 120034123776 bytes, 234441648 sectors
        result = regex_find(g, "Disk (\S+):", out)
        if result == False:

            logger.info("ERROR: Device Name is Invalid")
            sys.exit()
    g.device = result[0]
    logger.warning( "dev="+ g.device + " lbas=" + str(g.total_lbas) + " sec_size=" + str(g.sector_size))

    g.total_capacity_gib = g.total_lbas * g.sector_size / g.GiB
    printf("lbas: %d sec_size: %d total: %0.2f GiB\n", g.total_lbas, g.sector_size, g.total_capacity_gib)

    g.num_buckets = g.total_lbas * g.sector_size // g.bucket_size

### Draw heatmap on color terminal
def draw_heatmap(g):
    return
# draw_heatmap (TODO)

### Cleanup temp files
def cleanup_files(g):
    logger.warning( "Cleaning up temp files\n")
    for file in g.cleanup:
        logger.debug( file)
        os.system("rm -f " + file)
    os.system("rm -f filetrace.*.txt")
    return
# cleanup_files (DONE)

### run_cmd
def run_cmd(g, cmd):
    rc  = 0
    out = ""
    logger.debug( "cmd: " + cmd)
    args = shlex.split(cmd)
    
    try:
        p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except:
        logger.info("ERROR: problem with Popen")
        sys.exit(1)
    try: 
        out, error = p.communicate()
    except:
        logger.info("ERROR: problem with Popen.communicate()")
    rc = p.returncode
    if rc == 0:
        logger.debug( "rc=" + str(p.returncode))
    return (rc, out)
        
# run_cmd

### regex_find
def regex_find(g, pattern, input):
    output = False
    #logger.debug(f"PATTERN: {pattern}")
    for line in input.split("\n"):
        #logger.debug(f"LINE: {line}")
        match = re.search(pattern, line)
        if match != None:
            output =  match.groups()
            #logger.debug('MATCHED pattern')
            break
    return output
# regex_find

def printf(format, *args):
    sys.stdout.write(format % args)
# printf (DONE)

def setup_logger(logger):
    # Create and configure logger
    logging.basicConfig(
        level=logging.DEBUG,
        format=log_format,
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )
    # Creating an object
    logger = logging.getLogger()

    # Setting the threshold of logger to INFO
    logger.setLevel(logging.INFO)
    return logger

### MAIN
def main(argv):
    global logger
    logger = setup_logger(logger)

    # Globals
    g = global_variables()
    logger.info(f"VERSION: {g.version}")

    #check_args(g, argv) # BEN Remove
    command_args = get_arguments(argv)
    set_globals(g, command_args)

    if g.mode == 'live' or g.mode == 'trace':
        mount_debugfs(g)

    if g.mode == 'trace':
        # Trace

        # Check sudo permissions
        rc = os.system("sudo -v &>/dev/null")
        if rc != 0:
            logger.info("ERROR: You need to have sudo permissions to collect all necessary data.  Please run from a privilaged account.")
            sys.exit(6)
        # Save fdisk info
        logger.debug( "Running fdisk")
        fdisk_version = ""
        (rc, fdisk_version) = run_cmd(g, "fdisk -v")
        logger.debug( fdisk_version)
        fdisk_version = fdisk_version.decode("utf-8")
        match = re.search("util-linux-ng", fdisk_version)
        if match:
            # RHEL 6.x
            rc = os.system("sudo fdisk -ul "+g.device+" > fdisk."+g.device_str)
        else:
            # RHEL 7.x
            rc = os.system("sudo fdisk -l -u=sectors "+g.device+" > fdisk."+g.device_str)
        if rc != 0:
            logger.error(f"fdisk failed: {rc}")
            sys.exit(1)

        os.system("rm -f blk.out.* &>/dev/null") # Cleanup previous mess
        runcount = g.runtime / g.timeout
        while runcount > 0:
            time_left = runcount * g.timeout
            percent_prog = (g.runtime - time_left) * 100  / g.runtime
            printf( "\r%d %% done (%d seconds left)", percent_prog, time_left)
            #sys.stdout.flush() # BEN
            cmd = "sudo blktrace -b " + str(g.buffer_size) + " -n " + str(g.buffer_count) + " -a queue -d " + str(g.device) + " -o blk.out." + str(g.device_str) + ".0 -w " + str(g.timeout) + " 1> /dev/null"
            logger.debug( cmd)

            rc = os.system(cmd)

            if rc != 0:
                logger.info("Unable to run the 'blktrace' tool required to trace all of your I/O")
                logger.info("If you are using SLES 11 SP1, then it is likely that your default kernel is missing CONFIG_BLK_DEV_IO_TRACE")
                logger.info("which is required to run blktrace.  This is only available in the kernel-trace version of the kernel.")
                logger.info("kernel-trace is available on the SLES11 SP1 DVD and you simply need to install this and boot to this")
                logger.info("kernel version in order to get this working.")
                logger.info("If you are using a differnt distro or custom kernel, you may need to rebuild your kernel with the 'CONFIG_BLK 1f40 _DEV_IO_TRACE'")
                logger.info("option enabled.  This should allow blktrace to function\n")
                logger.info("ERROR: Could not run blktrace")
                sys.exit(7)
            cmd = "sudo blkparse -i blk.out." + g.device_str + ".0 -q -f " + '" %d %a %S %n\n" | grep -v cfq | gzip --fast > blk.out.' + g.device_str + ".0.blkparse.gz;"
            logger.debug( cmd)
            rc = os.system(cmd)
            if rc != 0:
                logger.error(f"blkparse returned non-zero return code rc={rc}")
            runcount -= 1
        logger.info("\rMapping files to block locations                ")

        if g.trace_files:
            find_all_files(g)
        tarball_name = g.device_str + ".tar"
        logger.info("\rCreating tarball " + tarball_name)
        filetrace = ""
        if g.trace_files:
            filetrace = "filetrace." + g.device_str + ".*.txt.gz"
        cmd = "tar -cf " + tarball_name + " blk.out." + g.device_str + ".*.gz fdisk." + g.device_str + " " + filetrace + " 1> /dev/null"
        logger.info(cmd)
        rc = os.system(cmd)
        if rc != 0:
            logger.info("ERROR: failed to tarball " + tarball_name)
            sys.exit(8)
        cmd = "rm -f blk.out." + g.device_str + ".*.gz; rm -f fdisk." + g.device_str + "; rm -f filetrace." + g.device_str + ".*.gz"
        rc = os.system(cmd)
        logger.info("\rFINISHED tracing: " + tarball_name)
        name = os.path.basename(__file__)
        logger.info("Please use this file with python3 " + name + " -m post -t " + tarball_name + " to create a report")

    elif g.mode == 'post':
        # Post 
        #g.THREAD_MAX = multiprocessing.cpu_count() * 4

        # CPU Count
        cpu_count = multiprocessing.cpu_count()
        proc_pool = Pool(cpu_count)

        input_tar_files(g)

        # Make the PDF plot a square matrix to keep gnuplot happy
        g.y_height = g.x_width = int(math.sqrt(g.num_buckets))
        logger.debug( "x=" + str(g.x_width) + " y=" + str(g.y_height))

        #g.debug=True
        logger.debug( "num_buckets=" + str(g.num_buckets) + " sector_size=" + str(g.sector_size) + " total_lbas=" + str(g.total_lbas) + " bucket_size=" + str(g.bucket_size))
        #g.debug=False
        rc = os.system("rm -f filetrace." + g.device_str + ".*.txt")
        rc = os.system("rm -f blk.out." + g.device_str + ".*.blkparse")
        logger.info("Time to parse.  Please wait...\n")

        size = len(g.file_list)
        file_count = 0

        plist = []
        for filename in g.file_list:
            logger.debug(filename)
            logger.debug("----------------------")
            file_count += 1
            #perc = file_count * 100 / size
            printf("\rInput Percent: %d %% (File %d of %d) threads=%d", (file_count*100 / size), file_count, size, len(plist))
            sys.stdout.flush()
            result = regex_find(g, "(blk.out.\S+).gz", filename)
            if result != False:
                new_file = result[0]
                #if g.single_threaded:
                if True:
                    thread_parse(g, new_file, file_count)
                    logger.debug( "blk.out hit = " + filename + "\n")
                else:
                    p = Process(target=thread_parse, args=(g, new_file, file_count))
                    plist.append(p)
                    p.start()
            result = regex_find(g, "(filetrace.\S+.\S+.txt).gz", filename)
            if result != False:
                new_file = result[0]
                g.trace_files=True
                logger.debug( "filetrace hit = " + filename+ "\n")
                if g.single_threaded:
                    parse_filetrace(g, new_file, file_count)
                    logger.debug( "blk.out hit = " + filename + "\n")
                else:
                    p = Process(target=parse_filetrace, args=(g, new_file, file_count))
                    plist.append(p)
                    p.start()
            while len(plist) > g.thread_max:
                for p in plist:
                    try:
                        p.join(0)
                    except:
                        pass
                    else:
                        if not p.is_alive():
                            plist.remove(p)
                time.sleep(0.10)

        if g.single_threaded == False:
            x=1
            while len(plist) > 0:
                dots=""
                for i in range(x):
                    dots = dots + "."
                x+=1
                if x>3:
                    x=1
                printf("\rWaiting on %3d threads to complete processing%-3s", len(plist), dots)
                printf("    ")
                sys.stdout.flush()
                for p in plist:
                    try:
                        p.join(0)
                    except:
                        pass
                    else:
                        if not p.is_alive():
                            plist.remove(p)
                time.sleep(0.10)

        logger.info("\rFinished parsing files.  Now to analyze         \n")
        file_to_buckets(g)
        print_results(g)
        print_stats(g)
        draw_heatmap(g)
        if g.pdf == True:
            print_header_heatmap(g)
            print_header_histogram_iops(g)
            print_header_stats_iops(g)
            create_report(g)
        cleanup_files(g)
        
    elif g.mode == 'live':
        # Live
        print ("Live Mode - Coming Soon ...")

    sys.exit()
# main (IN PROGRESS)

### Start MAIN
if __name__ == "__main__":
    main(sys.argv[1:])
