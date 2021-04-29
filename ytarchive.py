#!/usr/bin/env python3
from enum import Enum
import getopt
import http.cookiejar
import json
import logging
import os
import queue
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import threading
import urllib.parse
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET


ABOUT = {
	"name": "ytarchive",
	"version": "0.2.1",
	"date": "2021/01/27",
	"description": "Download youtube livestreams, from the beginning.",
	"author": "Kethsar",
	"license": "MIT",
	"url": "https://github.com/Kethsar/ytarchive"
}

'''
	TODO: Add more comments. Lots of code added without comments again.
'''

# Constants
INFO_URL = "https://www.youtube.com/get_video_info?video_id={0}&el=detailpage"
HTML_VIDEO_LINK_TAG = '<link rel="canonical" href="https://www.youtube.com/watch?v='
PLAYABLE_OK = "OK"
PLAYABLE_OFFLINE = "LIVE_STREAM_OFFLINE"
PLAYABLE_UNPLAYABLE = "UNPLAYABLE"
PLAYABLE_ERROR = "ERROR"
BAD_CHARS = '<>:"/\\|?*'
DTYPE_AUDIO = "audio"
DTYPE_VIDEO = "video"
DEFAULT_VIDEO_QUALITY = "best"
RECHECK_TIME = 15
FRAG_MAX_TRIES = 10
HOUR = 60 * 60
BUF_SIZE = 8192

# https://gist.github.com/AgentOak/34d47c65b1d28829bb17c24c04a0096f
AUDIO_ITAG = 140
VIDEO_LABEL_ITAGS = {
	"audio_only": 0, 
	"144p": {"h264": 160, "vp9": 278},
	"240p": {"h264": 133, "vp9": 242},
	"360p": {"h264": 134, "vp9": 243},
	"480p": {"h264": 135, "vp9": 244},
	"720p": {"h264": 136, "vp9": 247},
	"720p60": {"h264": 298, "vp9": 302},
	"1080p": {"h264": 137, "vp9": 248},
	"1080p60": {"h264": 299, "vp9": 303},
}

class Action(Enum):
	ASK = 0
	DO = 1
	DO_NOT = 2

# Simple class to more easily keep track of what fields are available for
# file name formatting
class FormatInfo:
	finfo = {
		"id": "",
		"title": "",
		"channel_id": "",
		"channel": "",
		"upload_date": ""
	}

	def get_info(self):
		return self.finfo

	def set_info(self, player_response):
		pmfr = player_response["microformat"]["playerMicroformatRenderer"]
		vid_details = player_response["videoDetails"]
		# "uploadDate" is actually when the livestream was created, not when it will start
		# Grab the actual start date from "startTimestamp"
		start_date = pmfr["liveBroadcastDetails"]["startTimestamp"].replace("-", "")

		self.finfo["id"] = sterilize_filename(vid_details["videoId"])
		self.finfo["title"] = sterilize_filename(vid_details["title"])
		self.finfo["channel_id"] = sterilize_filename(vid_details["channelId"])
		self.finfo["channel"] = sterilize_filename(vid_details["author"])
		self.finfo["upload_date"] = sterilize_filename(start_date[:8])

# Info to be sent through the progress queue
class ProgressInfo:
	def __init__(self, dtype, byte_count, max_seq):
		self.data_type = dtype
		self.bytes = byte_count
		self.max_seq = max_seq

# Fragment data
class Fragment:
	def __init__(self, seq, fname, header_seqnum):
		self.seq = seq
		self.fname = fname
		self.x_head_seqnum = header_seqnum

# Metadata for the final file
class MetaInfo:
	meta = {
		"title": "",
		"artist": "",
		"date": "",
		"comment": ""
	}

	def get_meta(self):
		return self.meta

	def set_meta(self, player_response):
		pmfr = player_response["microformat"]["playerMicroformatRenderer"]
		vid_details = player_response["videoDetails"]
		url = "https://www.youtube.com/watch?v={0}".format(vid_details["videoId"])
		start_date = pmfr["liveBroadcastDetails"]["startTimestamp"].replace("-", "")

		self.meta["title"] = vid_details["title"]
		self.meta["artist"] = vid_details["author"]
		self.meta["date"] = start_date[:8]
		# MP4 doesn't allow for a url metadata field
		# Just put it at the top of the description
		self.meta["comment"] = "{0}\n\n{1}".format(url, vid_details["shortDescription"])

class MediaDLInfo:
	def __init__(self):
		self.active_threads = 0
		self.download_url = ""
		self.base_fpath = ""
		self.data_type = ""

# Miscellaneous information
class DownloadInfo:
	def __init__(self):
		# Python may have the GIL but it's better to be safe
		# RLock so we can lock multiple times in the same thread without deadlocking
		self.lock = threading.RLock()
		self.format_info = FormatInfo()
		self.metadata = MetaInfo()

		self.stopping = False
		self.in_progress = False
		self.is_live = False
		self.vp9 = False
		self.is_unavailable = False
		self.gvideo_ddl = False

		self.thumbnail = ""
		self.vid = ""
		self.url = ""
		self.selected_quality = ""
		self.status = ""
		self.dash_manifest_url = ""

		self.wait = Action.ASK
		self.quality = -1
		self.retry_secs = 0
		self.thread_count = 1
		self.last_updated = 0
		self.target_duration = 5
		self.expires_in_seconds = 21540 # Usual 5h 59m expiration

		self.mdl_info = {
			DTYPE_VIDEO: MediaDLInfo(),
			DTYPE_AUDIO: MediaDLInfo()
		}

	def set_status(self, status):
		with self.lock:
			self.status = status
			self.print_status()

	# For use after logging statements, since they wipe out the current status
	# with how I have things set up
	def print_status(self):
		with self.lock:
			print(self.status, end="")

# Logging functions;
# ansi sgr 0=reset, 1=bold, while 3x sets the foreground color:
#   0black 1red 2green 3yellow 4blue 5magenta 6cyan 7white

def logerror(msg):
	logging.error("\033[31m{0}\033[0m\033[K".format(msg))

def logwarn(msg):
	logging.warning("\033[33m{0}\033[0m\033[K".format(msg))

def loginfo(msg):
	logging.info("\033[32m{0}\033[0m\033[K".format(msg))

def logdebug(msg):
	logging.debug("\033[36m{0}\033[0m\033[K".format(msg))

# Remove any illegal filename chars
# Not robust, but the combination of video title and id should prevent other illegal combinations
def sterilize_filename(fname):
	for c in BAD_CHARS:
		fname = fname.replace(c, "_")

	return fname

# Pretty formatting of byte count
def format_size(bsize):
	postfixes = ["bytes", "KiB", "MiB", "GiB"] # don't even bother with terabytes
	i = 0
	while bsize > 1024:
		bsize = bsize/1024
		i += 1
	
	return "{0:.2f}{1}".format(bsize, postfixes[i])

# Execute an external process using the given args
# Returns the process return code, or -1 on unknown error
def execute(args):
	retcode = 0
	logdebug("Executing command: {0}".format(" ".join(shlex.quote(x) for x in args)))

	try:
			retcode = subprocess.run(args, capture_output=True, check=True, encoding="utf-8").returncode
	except subprocess.CalledProcessError as err:
			retcode = err.returncode
			logerror(err.stderr)
	except Exception as err:
		logerror(err)
		retcode = -1

	return retcode

# Patch socket.getaddrinfo() to allow forcing IPv4 or IPv6
def patch_getaddrinfo(inet_family):
	orig_getaddrinfo = socket.getaddrinfo
	def new_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
		return orig_getaddrinfo(host=host, port=port, family=inet_family, type=type, proto=proto, flags=flags)
	socket.getaddrinfo = new_getaddrinfo

# Download data from the given URL and return it as unicode text
def download_as_text(url):
	data = b""

	try:
		with urllib.request.urlopen(url, timeout=5) as resp:
			data = resp.read()
	except Exception as err:
		logwarn("Failed to retrieve data from {0}: {1}".format(url, err))
		return None
	
	return data.decode("utf-8")

def download_thumbnail(url, fname):
	try:
		with urllib.request.urlopen(url, timeout=5) as resp:
			with open(fname, "wb") as f:
				f.write(resp.read())
	except Exception as err:
		logwarn("Failed to download thumbnail: {0}".format(err))
		return False

	return True

# Get the base player response object for the given video id
def get_player_response(info):
	vinfo = download_as_text(INFO_URL.format(info.vid))

	if not vinfo or len(vinfo) == 0:
		logwarn("No video information found, somehow")
		return None

	parsedinfo = urllib.parse.parse_qs(vinfo)
	player_response = json.loads(parsedinfo["player_response"][0])

	return player_response

# Make a comma-separated list of available formats
def make_quality_list(formats):
	qualities = ""
	quarity = ""

	for f in formats:
		qualities += f + ", "

	qualities += "best"
	return qualities

# Parse the user-given list of qualities they are willing to accept for download
def parse_quality_list(formats, quality):
	selected_qualities = []
	quality = quality.lower().strip()

	selected_quarities = quality.split("/")
	for q in selected_quarities:
		stripped = q.strip()
		if stripped in formats or stripped == "best":
			selected_qualities.append(q)
		
	if len(selected_qualities) < 1:
		print("No valid qualities selected")

	return selected_qualities

# Prompt the user to select a video quality
def get_quality_from_user(formats, waiting=False):
	if waiting:
		print("Since you are going to wait for the stream, you must pre-emptively select a video quality.")
		print("There is no way to know which qualities will be available before the stream starts, so a list of all possible stream qualities will be presented.")
		print("You can use youtube-dl style selection (slash-delimited first to last preference). Default is 'best'\n")
	
	quarity = ""
	selected_qualities = []
	qualities = make_quality_list(formats)
	print("Available video qualities: {0}".format(qualities))

	while len(selected_qualities) < 1:
		quarity = input("Enter desired video quality: ")
		quarity = quarity.lower().strip()
		if quarity == "":
			quarity = DEFAULT_VIDEO_QUALITY

		selected_qualities = parse_quality_list(formats, quarity)

	return selected_qualities

def get_yes_no(msg):
	yesno = input("{0} [y/N]: ".format(msg)).lower().strip()
	return yesno.startswith("y")

# Ask if the user wants to wait for a scheduled stream to start and then record it
def ask_wait_for_stream(info):
	print("{0} is probably a future scheduled livestream.".format(info.url))
	print("Would you like to wait for the scheduled start time, poll until it starts, or not wait?")
	choice = input("wait/poll/[no]: ").lower().strip()

	if choice.startswith("wait"):
		return True
	elif choice.startswith("poll"):
		secs = input("Input poll interval in seconds (15 or more recommended): ").strip()
		try:
			info.retry_secs = abs(int(secs))
		except Exception:
			logerror("Poll interval must be a whole number. Given {0}".format(secs))
			sys.exit(1)

		return True

	return False

# Keep retrieving the player response object until the playability status is OK
def get_playable_player_response(info):
	first_wait = True
	retry = True
	player_response = {}
	secs_late = 0
	selected_qualities = []

	if info.selected_quality:
		selected_qualities = parse_quality_list(list(VIDEO_LABEL_ITAGS.keys()), info.selected_quality)

	while retry:
		player_response = get_player_response(info)
		if not player_response:
			return None

		if not "videoDetails" in player_response:
			if info.in_progress:
				logwarn("Video details no longer available mid download.")
				logwarn("Stream was likely privated after finishing.")
				logwarn("We will continue to download, but if it starts to fail, nothing can be done.")
				info.print_status()
				info.is_live = False
				info.is_unavailable = True
			else:
				print("Video Details not found, video is likely private or does not exist.")
			return None

		if not player_response["videoDetails"]["isLiveContent"]:
			print("{0} is not a livestream. It would be better to use youtube-dl to download it.".format(info.url))
			return None

		playability = player_response["playabilityStatus"]
		playability_status = playability["status"]

		if playability_status == PLAYABLE_ERROR:
			logwarn("Playability status: ERROR. Reason: {0}".format(playability["reason"]))
			if info.in_progress:
				loginfo("Finishing download")
				info.is_live = False
			return None

		elif playability_status == PLAYABLE_UNPLAYABLE:
			logged_in = not player_response["responseContext"]["mainAppWebResponseContext"]["loggedOut"]

			logwarn("Playability status: Unplayable.")
			logwarn("Reason: {0}".format(playability["reason"]))
			logwarn("Logged in status: {0}".format(logged_in))
			logwarn("If this is a members only stream, you provided a cookies.txt file, and the above 'logged in' status is not True, please try updating your cookies file.")
			logwarn("Also check if your cookies file includes '#HttpOnly_' in front of some lines. If it does, delete that part of those lines and try again.")

			if info.in_progress:
				info.print_status()
				info.is_live = False
				info.is_unavailable = True
			return None

		elif playability_status == PLAYABLE_OFFLINE:
			# We've already started downloading, stream might be experiencing issues
			if info.in_progress:
				logdebug("Livestream status is {0} mid-download".format(PLAYABLE_OFFLINE))
				return None

			if info.wait == Action.DO_NOT:
				print("Stream appears to be a future scheduled stream, and you opted not to wait.")
				return None

			if first_wait and info.wait == Action.ASK and info.retry_secs == 0:
				if not ask_wait_for_stream(info):
					return None

			if first_wait:
				print()
				if len(selected_qualities) < 1:
					selected_qualities = get_quality_from_user(list(VIDEO_LABEL_ITAGS.keys()), True)

			if info.retry_secs > 0:
				if first_wait:
					try:
						poll_delay_ms = playability["liveStreamability"]["liveStreamabilityRenderer"]["pollDelayMs"]
						poll_delay = int(int(poll_delay_ms)/1000)

						if info.retry_secs < poll_delay:
							info.retry_secs = poll_delay
					except:
						pass

					print("Waiting for stream, retrying every {0} seconds...".format(info.retry_secs))

				first_wait = False
				time.sleep(info.retry_secs)
				continue

			# Jesus fuck youtube, embed some more objects why don't you
			sched_time = int(playability["liveStreamability"]["liveStreamabilityRenderer"]["offlineSlate"]["liveStreamOfflineSlateRenderer"]["scheduledStartTime"])
			cur_time = int(time.time())
			slep_time = sched_time - cur_time

			if slep_time > 0:
				if not first_wait:
					if secs_late > 0:
						print()
					print("Stream rescheduled")

				first_wait = False
				secs_late = 0

				print("Stream starts in {0} seconds. Waiting for this time to elapse...".format(slep_time))

				# Loop it just in case a rogue sleep interrupt happens
				while slep_time > 0:
					# There must be a better way but whatever
					time.sleep(slep_time)
					cur_time = int(time.time())
					slep_time = sched_time - cur_time

					if slep_time > 0:
						logdebug("Woke up {0} seconds early. Continuing sleep...".format(slep_time))

				# We've waited until the scheduled time
				continue


			if first_wait:
				print("Stream should have started, checking back every {0} seconds".format(RECHECK_TIME))
				first_wait = False

			# If we get this far, the stream's scheduled time has passed but it's still not started
			# Check every 15 seconds
			time.sleep(RECHECK_TIME)
			secs_late += RECHECK_TIME
			print("\rStream is {0} seconds late...".format(secs_late), end="")
			continue

		elif playability_status != PLAYABLE_OK:
			if secs_late > 0:
				print()
				
			logwarn("Unknown playability status: {0}".format(playability_status))
			if info.in_progress:
				info.is_live = False

			return None

		if secs_late > 0:
			print()

		retry = False

	return {"player_response": player_response, "selected_qualities": selected_qualities}

def is_fragmented(url):
	# Per anon, there will be a noclen parameter if the given URLs
	# are meant to be downloaded in fragments. Else it will have a clen
	# parameter obviously specifying content length.
	return url.lower().find("noclen") >= 0

# Parse the DASH manifest XML and get the download URLs from it
def get_urls_from_manifest(manifest):
	urls = {}

	try:
		root = ET.fromstring(manifest)
		reps = root.findall(".//{*}Representation")

		for r in reps:
			itag = r.get("id")
			url = r.find("{*}BaseURL").text + "sq/{0}"

			try:
				int(itag)
			except Exception:
				continue

			if itag and url:
				urls[int(itag)] = url
	except Exception as err:
		logwarn("Error parsing DASH manifest: {0}".format(err))

	return urls

# Get download URLs either from the DASH manifest or from the adaptiveFormats
# Prioritize DASH manifest if it is available
def get_download_urls(info, formats):
	urls = {}

	if info.dash_manifest_url:
		manifest = download_as_text(info.dash_manifest_url)

		if manifest:
			urls = get_urls_from_manifest(manifest)
			
			if urls:
				return urls
	
	for fmt in formats:
		if "url" in fmt:
			urls[fmt["itag"]] = fmt["url"] + "&sq={0}"

	return urls

# Get necessary video info such as video/audio URLs
# Stores them in info
def get_video_info(info):
	with info.lock: # Because I forgot some releases, this is worth the extra indent
		if info.gvideo_ddl:
			# We have no idea if we can get the video information.
			# Don't even bother to avoid complexity. Might change later.
			return False

		if info.stopping:
			return False

		# We already know there's no information to be gotten
		if info.is_unavailable:
			return None

		# Almost nothing we care about is likely to change in 15 seconds,
		# except maybe whether the livestream is online
		update_delta = time.time() - info.last_updated
		if update_delta < RECHECK_TIME:
			return False

		vals = get_playable_player_response(info)
		if not vals:
			return False

		player_response = vals["player_response"]
		selected_qualities = vals["selected_qualities"]
		video_details = player_response["videoDetails"]
		streaming_data = player_response["streamingData"]
		pmfr = player_response["microformat"]["playerMicroformatRenderer"]
		live_details = pmfr["liveBroadcastDetails"]
		is_live = live_details["isLiveNow"]

		if not is_live and not info.in_progress:
			# Likely the livestream ended already.
			# Check if the stream has been mostly processed.
			# If not then download it. Else youtube-dl is a better choice.
			if "endTimestamp" in live_details:
				# Assume that all formats will be fully processed if one is, and vice versa
				if not "url" in streaming_data["adaptiveFormats"][0]:
					print("Livestream has ended and is being processed. Download URLs are not available.")
					return False

				url = streaming_data["adaptiveFormats"][0]["url"]
				if not is_fragmented(url):
					print("Livestream has been processed, use youtube-dl instead.")
					return False
			else:
				print("Livestream is offline, should have started, but has no end timestamp.")
				print("You could try again, or try youtube-dl.")
				return False
		
		if "dashManifestUrl" in streaming_data: # Should be but maybe it isn't sometimes
			info.dash_manifest_url = streaming_data["dashManifestUrl"]

		formats = streaming_data["adaptiveFormats"]
		info.target_duration = formats[0]["targetDurationSec"]
		dl_urls = get_download_urls(info, formats)

		if info.quality < 0:
			qualities = ["audio_only"]
			itags = list(VIDEO_LABEL_ITAGS.keys())
			found = False

			# Generate a list of available qualities, sorted in order from best to worst
			# Assuming if VP9 is available, h264 should be available for that quality too
			for fmt in formats:
				if fmt["mimeType"].startswith("video/mp4"):
					qlabel = fmt["qualityLabel"].lower()
					priority = itags.index(qlabel)
					idx = 0

					for q in qualities:
						p = itags.index(q)
						if p > priority:
							break
						
						idx += 1

					qualities.insert(idx, qlabel)

			while not found:
				if len(selected_qualities) == 0:
					selected_qualities = get_quality_from_user(qualities)

				for q in selected_qualities:
					q = q.strip()

					# Get the best quality of those availble.
					# This is why we sorted the list as we made it.
					if q == "best":
						q = qualities[len(qualities) - 1]

					video_itag = VIDEO_LABEL_ITAGS[q]
					aonly = video_itag == VIDEO_LABEL_ITAGS["audio_only"]
					info.mdl_info[DTYPE_AUDIO].download_url = dl_urls[AUDIO_ITAG]

					if aonly:
						info.quality = video_itag
						info.mdl_info[DTYPE_VIDEO].download_url = ""
						found = True
						break

					if info.vp9 and video_itag["vp9"] in dl_urls:
						info.mdl_info[DTYPE_VIDEO].download_url = dl_urls[video_itag["vp9"]]
						info.quality = video_itag["vp9"]
						found = True
						print("Selected quality: {0} (VP9)".format(q))
						break
					elif video_itag["h264"] in dl_urls:
						info.mdl_info[DTYPE_VIDEO].download_url = dl_urls[video_itag["h264"]]
						info.quality = video_itag["h264"]
						found = True
						print("Selected quality: {0} (h264)".format(q))
						break
			
				# None of the qualities the user gave were available
				# Should only be possible if they chose to wait for a stream
				# and chose only qualities that the streamer ended up not using
				# i.e. 1080p60/720p60 when the stream is only available in 30 FPS
				if not found:
					print("\nThe qualities you selected ended up unavailble for this stream")
					print("You will now have the option to select from the available qualities")
					selected_qualities.clear()
		else:
			aonly = info.quality == VIDEO_LABEL_ITAGS["audio_only"]

			# Don't bother with refreshing the URL if it's not the kind we can even use
			if AUDIO_ITAG in dl_urls and is_fragmented(dl_urls[AUDIO_ITAG]):
				info.mdl_info[DTYPE_AUDIO].download_url = dl_urls[AUDIO_ITAG]

			if not aonly:
				if info.quality in dl_urls and is_fragmented(dl_urls[info.quality]):
					info.mdl_info[DTYPE_VIDEO].download_url = dl_urls[info.quality]

		# Grab some extra info on the first run through this function
		if not info.in_progress:
			info.format_info.set_info(player_response)
			info.metadata.set_meta(player_response)
			info.thumbnail = pmfr["thumbnail"]["thumbnails"][0]["url"]
			info.in_progress = True

		info.expires_in_seconds = int(streaming_data["expiresInSeconds"])
		info.is_live = is_live
		info.last_updated = time.time()

	return True

# Get the name of top-level atoms along with their offset and length
# In our case, data should be the first 5kb - 8kb of a fragment
def get_atoms(data):
	atoms = {}
	ofs = 0

	while True:
		# We should be fine and not run into errors, but I do dumb things
		try:
			alen = int(data[ofs:ofs+4].hex(), 16)
			if alen > len(data):
				break

			aname = data[ofs+4:ofs+8].decode()
			atoms[aname] = {"ofs": ofs, "len": alen}
			ofs += alen
		except Exception:
			break

		if ofs+8 >= len(data):
			break

	return atoms

# Remove the sidx atom from a chunk of data
def remove_sidx(data):
	atoms = get_atoms(data)
	if not "sidx" in atoms:
		return data

	sidx = atoms["sidx"]
	ofs = sidx["ofs"]
	rlen = sidx["ofs"] + sidx["len"]
	new_data = data[:ofs] + data[rlen:]

	return new_data

# Download a fragment and send it back via data_queue
def download_frags(data_type, info, seq_queue, data_queue):
	downloading = True
	frag_tries = 0
	url = info.mdl_info[data_type].download_url
	tname = threading.current_thread().getName()

	while downloading:
		# Check if the user decided to cancel this download, and exit gracefully
		with info.lock:
			if info.stopping:
				break

		tries = 0
		full_retries = 3
		seq = -1
		max_seq = -1
		is_403 = False

		try:
			seq, max_seq = seq_queue.get(timeout=info.target_duration)
			frag_tries = 0
		except queue.Empty:
			# Check again in case the user opted to stop 
			with info.lock:
				if info.stopping:
					downloading = False
					break

			frag_tries += 1
			if frag_tries >= FRAG_MAX_TRIES:
				# For all instances where we might try to stop downloading,
				# make sure the livestream is not still live.
				# If it is, keep trying. Had all video download threads die
				# somehow while a stream was still going. Hopefully this
				# will fix that

				with info.lock:
					if info.mdl_info[data_type].active_threads > 1:
						logdebug("{0}: Starved for fragment numbers and multiple fragment threads running".format(tname))
						logdebug("{0}: Closing this thread to minimize unneeded network requests".format(tname))
						info.print_status()

						downloading = False
						continue

					if info.is_live:
						get_video_info(info)

					if not info.is_live:
						logdebug("{0}: Starved for fragment numbers and stream is offline".format(tname))
						downloading = False
					else:
						logdebug("{0}: Could not get a new fragment to download after {1} tries and we are the only active downloader".format(tname, FRAG_MAX_TRIES))
						logdebug("{0}: That is an issue, hopefully it will correct itself".format(tname))
						info.print_status()
						frag_tries = 0

			continue

		if max_seq > -1:
			with info.lock:
				if not info.is_live and seq >= max_seq:
					logdebug("{0}: Stream is finished and highest sequence reached".format(tname))
					downloading = False
					break

		fname = "{0}.frag{1}.ts".format(info.mdl_info[data_type].base_fpath, seq)

		while tries < FRAG_MAX_TRIES:
			with info.lock:
				if info.stopping:
					downloading = False
					break

			bytes_written = 0

			try:
				header_seqnum = -1
				with urllib.request.urlopen(url.format(seq), timeout=info.target_duration * 2) as resp:
					header_seqnum = int(resp.getheader("X-Head-Seqnum", -1))

					with open(fname, "wb") as frag_file:
						# Read response data into a file in BUF_SIZE chunks
						while True:
							buf = resp.read(BUF_SIZE)
							if len(buf) == 0:
								break

							bytes_written += frag_file.write(buf)

				# The request was a success but no data was given
				# Increment the try counter and wait
				if bytes_written == 0:
					tries += 1
					if tries < FRAG_MAX_TRIES:
						time.sleep(info.target_duration)
						continue
				else:
					data_queue.put(Fragment(seq, fname, header_seqnum))
					is_403 = False
					break
			except urllib.error.HTTPError as err:
				logdebug("{0}: HTTP Error for fragment {1}: {2}".format(tname, seq, err))
				info.print_status()

				# 403 means our URLs have likely expired
				if err.code == 403:
					# Check if a new URL is already waiting for us
					# Else refresh auth by calling get_video_info again
					is_403 = True

					if not info.gvideo_ddl: # Don't bother if gvideo links were the input
						logdebug("{0}: Attempting to retrieve a new download URL".format(tname))
						info.print_status()
						with info.lock:
							new_url = info.mdl_info[data_type].download_url

							if new_url != url:
								url = new_url
							elif get_video_info(info):
								url = info.mdl_info[data_type].download_url
				elif err.code == 404:
					if max_seq > -1:
						with info.lock:
							if not info.is_live and seq >= (max_seq - 2):
								logdebug("{0}: Stream has ended and fragment within the last two not found, probably not actually created".format(tname))
								info.print_status()
								downloading = False
								break
				
				tries += 1
				if tries < FRAG_MAX_TRIES:
					time.sleep(info.target_duration)
			except Exception as err:
				logdebug("{0}: Error with fragment {1}: {2}".format(tname, seq, err))
				info.print_status()

				if max_seq > -1:
					with info.lock:
						if not info.is_live and seq >= (max_seq - 2):
							logdebug("{0}: Stream has ended and fragment number is within two of the known max, probably not actually created".format(tname))
							downloading = False
							try_delete(fname)
							info.print_status()
							break

				tries += 1
				if tries < FRAG_MAX_TRIES:
					time.sleep(info.target_duration)

			if tries >= FRAG_MAX_TRIES:
				full_retries -= 1
				try_delete(fname)
				info.print_status()

				logdebug("{0}: Fragment {1}: {2}/{3} retries".format(
					tname,
					seq,
					tries,
					FRAG_MAX_TRIES
				))
				info.print_status()

				with info.lock:
					if info.is_live:
						get_video_info(info)

					if not info.is_live:
						if info.is_unavailable and is_403:
							logwarn("{0}: Download link likely expired and stream is privated or members only, cannot coninue download".format(tname))
							info.print_status()
							downloading = False
						elif max_seq > -1 and seq < (max_seq - 2) and full_retries > 0:
							logdebug("{0}: More than two fragments away from the highest known fragment".format(tname))
							logdebug("{0}: Will try grabbing the fragment {1} more times".format(tname, full_retries))
							info.print_status()
						else:
							downloading = False
					else:
						logdebug("{0}: Fragment {1}: Stream still live, continuing download attempt".format(tname, seq))
						info.print_status()
						tries = 0

	logdebug("{0}: exiting".format(tname))
	info.print_status()

	with info.lock:
		info.mdl_info[data_type].active_threads -= 1

# Download the given data_type stream to dfile
# Sends progress info through progress_queue
def download_stream(data_type, dfile, progress_queue, info):
	data_queue = queue.Queue()
	seq_queue = queue.Queue()
	cur_frag = 0
	cur_seq = 0
	active_downloads = 0
	max_seqs = -1
	tries = 10
	tnum = 0
	stopping = False
	dthreads = []
	data = []
	del_frags = []
	f = open(dfile, "wb")

	with info.lock:
		while info.mdl_info[data_type].active_threads < info.thread_count:
			t = threading.Thread(target=download_frags,
				args=(data_type, info, seq_queue, data_queue),
				name="{0}{1}".format(data_type, tnum))

			dthreads.append(t)
			info.mdl_info[data_type].active_threads += 1
			tnum += 1
			seq_queue.put((cur_seq, max_seqs))
			cur_seq += 1
			active_downloads += 1
			t.start()

	while True:
		downloading = False

		with info.lock:
			stopping = info.stopping

		for t in dthreads:
			if t.is_alive():
				downloading = True
				break

		# Get all available data and start another download for each data retrieved
		while True:
			try:
				d = data_queue.get_nowait()
				data.append(d)
				active_downloads -= 1

				# We want to empty the queue so we don't leave any files behind
				if not downloading or stopping:
					continue
				
				if d.x_head_seqnum > max_seqs:
					max_seqs = d.x_head_seqnum

				# If we know the current max sequence number, use that to
				# determine if we try for another fragment. Else just try anyway
				if max_seqs > 0:
					if cur_seq <= max_seqs + 1:
						# One higher than known max as we can download faster than
						# the fragments are made
						seq_queue.put((cur_seq, max_seqs))
						cur_seq += 1
						active_downloads += 1
				else:
					seq_queue.put((cur_seq, max_seqs))
					cur_seq += 1
					active_downloads += 1
			except queue.Empty:
				break

		if not downloading:
			break

		# Wait for 100ms if no data is available
		if len(data) == 0:
			if not stopping and active_downloads <= 0:
				logdebug("{0}-download: Somehow no active downloads and no data to write".format(data_type))
				logdebug("{0}-download: Fragment this happened at: {1}".format(data_type, cur_frag))
				info.print_status()

				with info.lock:
					while active_downloads < info.mdl_info[data_type].active_threads:
						seq_queue.put((cur_seq, max_seqs))
						cur_seq += 1
						active_downloads += 1

			time.sleep(0.1)
			continue

		# Write any fragments in the queue that are next for writing
		i = 0
		while i < len(data) and tries > 0:
			d = data[i]
			if not d.seq == cur_frag:
				i += 1
				continue

			try:
				bytes_written = 0

				with open(d.fname, "rb") as rf:
					# Remvoe sidx atoms from video and audio
					# Fixes an issue with streams encoded differently than normal
					buf = rf.read(BUF_SIZE)
					buf = remove_sidx(buf)
					bytes_written += f.write(buf)

					while True:
						buf = rf.read(BUF_SIZE)
						if len(buf) == 0:
							break

						bytes_written += f.write(buf)

				cur_frag += 1
				progress_queue.put(ProgressInfo(data_type, bytes_written, max_seqs))

				try:
					os.remove(d.fname)
				except Exception as err:
					logwarn("{0}-download: Error deleting fragment {1}: {2}".format(data_type, d.seq, err))
					logwarn("{0}-download: Will try again after the download has finished".format(data_type))
					del_frags.append(d.fname)
					info.print_status()

				data.remove(d)
				tries = 10
				i = 0 # Start from the beginning since the next one might have finished downloading earlier
			except Exception as err:
				tries -= 1
				logwarn("{0}-download: Error when attempting to write fragment {1} to {2}: {3}".format(data_type, cur_frag, dfile, err))
				info.print_status()

				if tries > 0:
					logwarn("{0}-download: Will try {1} more time(s)".format(data_type, tries))
					info.print_status()

			if stopping:
				continue

			# Threads closing prematurely possibly due to disk writes taking too long
			# Open them back up
			with info.lock:
				if (max_seqs - cur_seq) > 100 and info.mdl_info[data_type].active_threads < info.thread_count:
					logdebug("{0}-download: More than 100 fragments below the current max and less than the max threads are running".format(data_type))
					logdebug("{0}-download: Starting more threads".format(data_type))

					while info.mdl_info[data_type].active_threads < info.thread_count:
						t = threading.Thread(target=download_frags,
							args=(data_type, info, seq_queue, data_queue),
							name="{0}{1}".format(data_type, tnum))

						dthreads.append(t)
						info.mdl_info[data_type].active_threads += 1
						tnum += 1
						seq_queue.put((cur_seq, max_seqs))
						cur_seq += 1
						active_downloads += 1
						t.start()

		# Refresh the info every hour to keep our download URLs up to date
		# Might not actually be that helpful but will prevent last-second
		# expiration while still downloading a stream that was privated after ending
		with info.lock:
			updated_secs = time.time() - info.last_updated
			if not info.is_unavailable and updated_secs > HOUR:
				get_video_info(info)

		if tries <= 0:
			logwarn("{0}-download: Stopping download, something must be wrong...".format(data_type))
			info.print_status()

			with info.lock:
				info.stopping = True

			for t in dthreads:
				t.join()

	if not f.closed:
		f.close()

	# Remove any files likely the result of an early termination
	if len(data) > 0:
		for d in data:
			try_delete(d.fname)
	
	# Attempt to remove any files that failed to be removed earlier
	if len(del_frags) > 0:
		loginfo("{0}-download: Attempting to delete fragments that failed to be deleted before".format(data_type))
		for d in del_frags:
			try_delete(d)

	logdebug("{0}-download thread closing".format(data_type))
	info.print_status()

# For use with --video-url and --audio-url params mostly
def parse_gvideo_url(url, dtype):
	nurl = ""
	parsedurl = urllib.parse.urlparse(url)
	nl = parsedurl.netloc.lower()
	parsed_query = urllib.parse.parse_qs(parsedurl.query)
	itag = int(parsed_query["itag"][0])
	sq_idx = url.find("&sq=")

	if not nl.endswith(".googlevideo.com"):
		return nurl
	elif not "noclen" in parsed_query:
		print("Given Google Video URL is not for a fragmented stream.")
		return nurl
	elif dtype == DTYPE_AUDIO and itag != AUDIO_ITAG:
		print("Given audio URL does not have the audio itag. Make sure you set the correct URL(s)")
		return nurl
	elif dtype == DTYPE_VIDEO and itag == AUDIO_ITAG:
		print("Given video URL has the audio itag set. Make sure you set the correct URL(s)")
		return nurl

	nurl = url[:sq_idx] + "&sq={0}"
	return nurl

def get_gvideo_url(info, dtype):
	while True:
		url = input("Please enter the {0} url, or nothing to skip: ".format(dtype))
		if not url:
			if dtype != DTYPE_AUDIO:
				return
			else:
				print("Audio URL must be given. Video-only downloading is not supported at this time.")
				continue

		parsedurl = urllib.parse.urlparse(url)
		parsed_query = urllib.parse.parse_qs(parsedurl.query)
		itag = int(parsed_query["itag"][0])
		sq_idx = url.find("&sq=")
		if dtype == DTYPE_VIDEO:
			info.quality = itag

		if not "noclen" in parsed_query:
			print("Given Google Video URL is not for a fragmented stream.")
		# Lazy matching of URL to data type
		elif ((dtype == DTYPE_AUDIO and itag == AUDIO_ITAG)
			or (dtype == DTYPE_VIDEO and itag != AUDIO_ITAG)):
			info.mdl_info[dtype].download_url = url[:sq_idx] + "&sq={0}"
			break
		else:
			print("URL given does not appear to be appropriate for the data type needed.")

# Find the video ID from the given URL
def parse_input_url(info):
	parsedurl = urllib.parse.urlparse(info.url)
	nl = parsedurl.netloc.lower()
	lpath = parsedurl.path.lower()
	parsed_query = urllib.parse.parse_qs(parsedurl.query)

	if nl == "www.youtube.com" or nl == "youtube.com":
		if lpath.startswith("/watch"):
			if not "v" in parsed_query:
				logerror("Youtube URL missing video ID")
				return

			# parsed queries are always in a list
			info.vid = parsed_query["v"][0]

		# Attempt to find the actual video ID of the current or closest scheduled
		# livestream for a channel
		elif lpath.startswith("/channel") and lpath.endswith("live"):
			# This is fucking awful but it works
			html = download_as_text(info.url)
			if len(html) == 0:
				return

			startidx = html.find(HTML_VIDEO_LINK_TAG)
			if startidx < 0:
				return
			
			startidx += len(HTML_VIDEO_LINK_TAG)
			endidx = html.find('"', startidx)
			info.vid = html[startidx:endidx]
	elif nl == "youtu.be":
		# path includes the leading slash
		info.vid = parsedurl.path.strip("/")
	elif nl.endswith(".googlevideo.com"):
		# Special case. Receiving a direct googlevideo URL likely means it will
		# be the download URL and we cannot retrieve new ones or video information
		if not "noclen" in parsed_query:
			print("Given Google Video URL is not for a fragmented stream.")
			return
		
		info.gvideo_ddl = True
		info.vid = parsed_query["id"][0].rstrip(".1") # googlevideo id param has .1 at the end for some reason
		info.format_info.get_info()["id"] = info.vid # We cannot retrieve format info as normal. Set ID here
		itag = int(parsed_query["itag"][0])
		sq_idx = info.url.find("&sq=")

		if itag == AUDIO_ITAG:
			if not info.mdl_info[DTYPE_AUDIO].download_url:
				info.mdl_info[DTYPE_AUDIO].download_url = info.url[:sq_idx] + "&sq={0}"

			if not info.mdl_info[DTYPE_VIDEO].download_url and info.quality < 0:
				get_gvideo_url(info, DTYPE_VIDEO)
		else: # video url, presumably
			if not info.mdl_info[DTYPE_VIDEO].download_url:
				info.mdl_info[DTYPE_VIDEO].download_url = info.url[:sq_idx] + "&sq={0}"

			if not info.mdl_info[DTYPE_AUDIO].download_url:
				get_gvideo_url(info, DTYPE_AUDIO)

		info.quality = itag
	else:
		print("{0} is not a known valid youtube URL.".format(info.url))

# Attempt to move src_file to dst_file
def try_move(src_file, dst_file):
	try:
		if os.path.exists(src_file):
			loginfo("Moving file {0} to {1}".format(src_file, dst_file))
			os.replace(src_file, dst_file)
	except Exception as err:
		logwarn("Error moving file: {0}".format(err))

# Attempt to delete the given file
def try_delete(fname):
	try:
		if os.path.exists(fname):
			loginfo("Deleting file {0}".format(fname))
			os.remove(fname)
	except FileNotFoundError:
		pass
	except Exception as err:
		logwarn("Error deleting file: {0}".format(err))

def cleanup_files(files):
	for f in files:
		try_delete(f)

def print_help():
	fname = os.path.basename(sys.argv[0])

	print()
	print("usage: {0} [OPTIONS] [url] [quality]".format(fname))
	print()

	print("\t[url] is a youtube livestream URL. If not provided, you will be")
	print("\tprompted to enter one.")
	print()

	print("\t[quality] is a slash-delimited list of video qualities you want")
	print("\tto be selected for download, from most to least wanted. If not")
	print("\tprovided, you will be prompted for one, with a list of available")
	print("\tqualities to choose from. The following values are valid:")
	print("\t{0}".format(make_quality_list(VIDEO_LABEL_ITAGS)))
	print()

	print("Options:")
	print("\t-h, --help")
	print("\t\tShow this help message.")
	print()

	print("\t-4, --ipv4")
	print("\t\tMake all connections using IPv4.")
	print()

	print("\t-6, --ipv6")
	print("\t\tMake all connections using IPv6.")
	print()

	print("\t--add-metadata")
	print("\t\tWrite some basic metadata information to the final file.")
	print()

	print("\t--audio-url GOOGLEVIDEO_URL")
	print("\t\tPass in the given url as the audio fragment url. Must be a")
	print("\t\tGoogle Video url with an itag parameter of 140.")
	print()

	print("\t-c, --cookies COOKIES_FILE")
	print("\t\tGive a cookies.txt file that has your youtube cookies. Allows")
	print("\t\tthe script to access members-only content if you are a member")
	print("\t\tfor the given stream's user. Must be netscape cookie format.")
	print()

	print("\t--debug")
	print("\t\tPrint a lot of extra information.")
	print()

	print("\t--merge")
	print("\t\tAutomatically run the ffmpeg command for the downloaded streams")
	print("\t\twhen sigint is received. You will be prompted otherwise.")
	print()

	print("\t--no-merge")
	print("\t\tDo not run the ffmpeg command for the downloaded streams")
	print("\t\twhen sigint is received. You will be prompted otherwise.")
	print()

	print("\t--no-save")
	print("\t\tDo not save any downloaded data and files if not having ffmpeg")
	print("\t\trun when sigint is received. You will be prompted otherwise.")
	print()

	print("\t--no-video")
	print("\t\tIf a googlevideo url is given or passed with --audio-url, do not")
	print("\t\tprompt for a video url. If a video url is given with --video-url")
	print("\t\tthen this is effectively ignored.")
	print()

	print("\t-n, --no-wait")
	print("\t\tDo not wait for a livestream if it's a future scheduled stream.")
	print()

	print("\t-o, --output FILENAME_FORMAT")
	print("\t\tSet the output file name EXCLUDING THE EXTENSION. Can include")
	print("\t\tformatting similar to youtube-dl, albeit much more limited.")
	print("\t\tSee FORMAT OPTIONS below for a list of available format keys.")
	print("\t\tDefault is '%(title)s-%(id)s'")
	print()

	print("\t-r, --retry-stream SECONDS")
	print("\t\tIf waiting for a scheduled livestream, re-check if the stream is")
	print("\t\tup every SECONDS instead of waiting for the initial scheduled time.")
	print("\t\tIf SECONDS is less than the poll delay youtube gives (typically")
	print("\t\t15 seconds), then this will be set to the value youtube provides.")
	print()

	print("\t--save")
	print("\t\tAutomatically save any downloaded data and files if not having")
	print("\t\tffmpeg run when sigint is received. You will be prompted otherwise.")
	print()

	print("\t--threads THREAD_COUNT")
	print("\t\tSet the number of threads to use for downloading audio and video")
	print("\t\tfragments. The total number of threads running will be")
	print("\t\tTHREAD_COUNT * 2 + 3. Main thread, a thread for each audio and")
	print("\t\tvideo download, and THREAD_COUNT number of fragment downloaders")
	print("\t\tfor both audio and video. The nature of Python means this script")
	print("\t\twill never use more than a single CPU core no matter how many")
	print("\t\tthreads are started. Setting this above 5 is not recommended.")
	print("\t\tDefault is 1.")
	print()

	print("\t-t, --thumbnail")
	print("\t\tDownload and embed the stream thumbnail in the finished file.")
	print("\t\tWhether the thumbnail shows properly depends on your file browser.")
	print("\t\tWindows' seems to work. Nemo on Linux seemingly does not.")
	print()

	print("\t-v, --verbose")
	print("\t\tPrint extra information.")
	print()

	print("\t--video-url GOOGLEVIDEO_URL")
	print("\t\tPass in the given url as the video fragment url. Must be a")
	print("\t\tGoogle Video url with an itag parameter that is not 140.")
	print()

	print("\t--vp9")
	print("\t\tIf there is a VP9 version of your selected video quality,")
	print("\t\tdownload that instead of the usual h264.")
	print()

	print("\t-w, --wait")
	print("\t\tWait for a livestream if it's a future scheduled stream.")
	print("\t\tIf this option is not used when a scheduled stream is provided,")
	print("\t\tyou will be asked if you want to wait or not.")
	print()

	print("\t--write-description")
	print("\t\tWrite the video description to a separate .description file.")
	print()

	print("\t--write-thumbnail")
	print("\t\tWrite the thumbnail to a separate file.")
	print()

	print("Examples:")
	print("\t{0} -w".format(fname))
	print("\t{0} -w https://www.youtube.com/watch?v=CnWDmKx9cQQ 1080p60/best".format(fname))
	print("\t{0} --threads 3 https://www.youtube.com/watch?v=ZK1GXnz-1Lw best".format(fname))
	print("\t{0} --wait -r 30 https://www.youtube.com/channel/UCZlDXzGoo7d44bwdNObFacg/live best".format(fname))
	print("\t{0} -c cookies-youtube-com.txt https://www.youtube.com/watch?v=_touw1GND-M best".format(fname))
	print("\t{0} --no-wait --add-metadata https://www.youtube.com/channel/UCvaTdHTWBGv3MKj3KVqJVCw/live best".format(fname))
	print("\t{0} -o '%(channel)s/%(upload_date)s_%(title)s' https://www.youtube.com/watch?v=HxV9UAMN12o best".format(fname))
	print()
	print()

	print("FORMAT OPTIONS")
	print("\tFormat keys provided are made to be the same as they would be for")
	print("\tyoutube-dl. See https://github.com/ytdl-org/youtube-dl#output-template")
	print()
	
	print("\tid (string): Video identifier")
	print("\ttitle (string): Video title")
	print("\tchannel_id (string): ID of the channel")
	print("\tchannel (string): Full name of the channel the livestream is on")
	print("\tupload_date (string): Technically stream date, UTC timezone (YYYYMMDD)")

def main():
	os.system("")  # enable vt100 on win10 >= 1607
	info = DownloadInfo()
	opts = None
	args = None
	cfile = ""
	fname_format = "%(title)s-%(id)s"
	thumbnail = False
	add_meta = False
	write_desc = False
	write_thumb = False
	verbose = False
	debug = False
	inet_family = 0
	merge_on_cancel = Action.ASK
	save_on_cancel = Action.ASK
	files = []

	try:
		opts, args = getopt.getopt(sys.argv[1:],
			"hwntv46c:r:o:",
			[
				"help",
				"wait",
				"no-wait",
				"thumbnail",
				"verbose",
				"debug",
				"vp9",
				"add-metadata",
				"ipv4",
				"ipv6",
				"write-description",
				"write-thumbnail",
				"merge",
				"no-merge",
				"save",
				"no-save",
				"no-video",
				"cookies=",
				"retry-stream=",
				"output=",
				"threads=",
				"video-url=",
				"audio-url="
			]
		)
	except getopt.GetoptError as err:
		logerror("{0}".format(err))
		print_help()
		sys.exit(1)

	for o, a in opts:
		if o in ("-h", "--help"):
			print_help()
			sys.exit(0)
		elif o in ("-w", "--wait"):
			info.wait = Action.DO
		elif o in ("-n", "--no-wait"):
			info.wait = Action.DO_NOT
		elif o == "--merge":
			merge_on_cancel = Action.DO
		elif o == "--no-merge":
			merge_on_cancel = Action.DO_NOT
		elif o == "--save":
			save_on_cancel = Action.DO
		elif o == "--no-save":
			save_on_cancel = Action.DO_NOT
		elif o == "--no-video":
			info.quality = AUDIO_ITAG
		elif o in ("-t", "--thumbnail"):
			thumbnail = True
		elif o in ("-v", "--verbose"):
			verbose = True
		elif o == "--vp9":
			info.vp9 = True
		elif o == "--debug":
			debug = True
		elif o == "--add-metadata":
			add_meta = True
		elif o == "--write-description":
			write_desc = True
		elif o == "--write-thumbnail":
			write_thumb = True
		elif o in ("-4", "--ipv4"):
			inet_family = socket.AF_INET
		elif o in ("-6", "--ipv6"):
			inet_family = socket.AF_INET6
		elif o in ("-c", "--cookies"):
			cfile = a
		elif o in ("-o", "--output"):
			fname_format = a
		elif o == "--video-url":
			url = parse_gvideo_url(a, DTYPE_VIDEO)
			if not url:
				print("Invalid video URL given with --video-url")
				sys.exit(1)
			
			info.mdl_info[DTYPE_VIDEO].download_url = url
		elif o == "--audio-url":
			url = parse_gvideo_url(a, DTYPE_AUDIO)
			if not url:
				print("Invalid audio URL given with --audio-url")
				sys.exit(1)
			
			info.mdl_info[DTYPE_AUDIO].download_url = url
		elif o in ("-r", "--retry-stream"):
			try:
				info.retry_secs = abs(int(a)) # Just abs it, don't bother dealing with negatives
			except Exception:
				logerror("--retry-stream must be given a whole number argument. Given {0}".format(a))
				sys.exit(1)
		elif o == "--threads":
			try:
				info.thread_count = abs(int(a))
			except Exception:
				logerror("--threads must be given a whole number argument. Given {0}".format(a))
				sys.exit(1)
		else:
			assert False, "Unhandled option"

	# Set up logging
	loglevel = logging.WARNING
	if debug:
		loglevel = logging.DEBUG
	elif verbose:
		loglevel = logging.INFO

	logging.basicConfig(format="\r%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S", level=loglevel)

	patch_getaddrinfo(inet_family)

	if info.mdl_info[DTYPE_VIDEO].download_url:
		info.url = info.mdl_info[DTYPE_VIDEO].download_url
	elif info.mdl_info[DTYPE_AUDIO].download_url:
		info.url = info.mdl_info[DTYPE_AUDIO].download_url

	if not info.url:
		if len(args) > 1:
			info.url = args[0]
			info.selected_quality = args[1]
		elif len(args) > 0:
			info.url = args[0]
		else:
			info.url = input("Enter a youtube livestream URL: ")

	parse_input_url(info)
	if not info.vid:
		logerror("Could not find video ID")
		sys.exit(1)

	# Test filename format to make sure a valid one was given
	try:
		fname_format % info.format_info.get_info()
	except KeyError as err:
		logerror("Unknown output format key: {0}".format(err))
		sys.exit(1)
	except Exception as err:
		logerror("Output format test failed: {0}".format(err))
		sys.exit(1)

	# Cookie handling for members-only streams
	if cfile:
		cjar = http.cookiejar.MozillaCookieJar(cfile)
		try:
			cjar.load()
			loginfo("Loaded cookie file {0}".format(cfile))
		except Exception as err:
			logerror("Failed to load cookies file: {0}".format(err))
			sys.exit(1)
		
		cproc = urllib.request.HTTPCookieProcessor(cjar)
		opener = urllib.request.build_opener(cproc)
		urllib.request.install_opener(opener)

	if not info.gvideo_ddl and not get_video_info(info):
		sys.exit(1)

	# Setup file name and directories
	full_fpath = fname_format % info.format_info.get_info()
	fdir = os.path.dirname(full_fpath)
	# Strip os.path.sep to prevent attempting to save to root in the event
	# that the formatting info is missing a param used as a top-level dir
	if not fname_format.startswith(os.path.sep):
		fdir = fdir.lstrip(os.path.sep)
	fname = os.path.basename(full_fpath)
	fname = sterilize_filename(fname)

	# ffmpeg was seeing - and trying to use the file name as an arg
	if fname.startswith("-"):
		fname = "_" + fname

	if len(fname.strip()) == 0:
		logerror("Output file name appears to be empty.")
		logerror("Expanded output file path: {0}".format(full_fpath))
		sys.exit(1)

	# Output format included a directory structure. Create it if it doesn't exist
	if fdir:
		try:
			os.makedirs(fdir, exist_ok=True)
		except Exception as err:
			logwarn("Error creating final file directory: {0}".format(err))
			logwarn("The final file will be placed in the current working directory")
			fdir = ""

	tmpdir = tempfile.TemporaryDirectory(prefix="{0}__".format(info.vid), dir=fdir)

	afile_name = "{0}.f{1}".format(fname, AUDIO_ITAG)
	vfile_name = "{0}.f{1}".format(fname, info.quality)
	thmbnl_file_name = "{0}.jpg".format(fname)
	desc_file_name = "{0}.description".format(fname)

	info.mdl_info[DTYPE_AUDIO].base_fpath = os.path.join(tmpdir.name, afile_name)
	info.mdl_info[DTYPE_VIDEO].base_fpath = os.path.join(tmpdir.name, vfile_name)

	afile = info.mdl_info[DTYPE_AUDIO].base_fpath + ".ts"
	vfile = info.mdl_info[DTYPE_VIDEO].base_fpath + ".ts"
	thmbnl_file = os.path.join(tmpdir.name, thmbnl_file_name)
	desc_file = os.path.join(tmpdir.name, desc_file_name)
	
	progress_queue = queue.Queue()
	total_bytes = 0
	threads = []
	frags = {
		DTYPE_AUDIO: 0,
		DTYPE_VIDEO: 0
	}

	# Grab the thumbnail for the livestream for embedding later
	if (thumbnail or write_thumb) and info.thumbnail:
		downloaded = download_thumbnail(info.thumbnail, thmbnl_file)

		# Failed to download but file itself got created. Remove it
		if not downloaded and os.path.exists(thmbnl_file):
			try_delete(thmbnl_file)
			thumbnail = False
			write_thumb = False
	else:
		thumbnail = False
		write_thumb = False

	if write_desc and info.metadata.get_meta()["comment"]:
		with open(desc_file, "w", encoding="utf-8") as f:
			f.write(info.metadata.get_meta()["comment"])
	
	loginfo("Starting download to {0}".format(afile))
	athread = threading.Thread(target=download_stream, args=(DTYPE_AUDIO, afile, progress_queue, info))
	threads.append(athread)
	athread.start()

	if info.mdl_info[DTYPE_VIDEO].download_url:
		loginfo("Starting download to {0}".format(vfile))
		vthread = threading.Thread(target=download_stream, args=(DTYPE_VIDEO, vfile, progress_queue, info))
		threads.append(vthread)
		vthread.start()

	# Print progress to stdout
	# Included info is video and audio fragments downloaded, and total data downloaded
	max_seqs = -1
	while True:
		alive = False

		for t in threads:
			if t.is_alive():
				alive = True
				break

		try:
			progress = progress_queue.get(timeout=1)
			total_bytes += progress.bytes
			frags[progress.data_type] += 1

			if progress.max_seq > max_seqs:
				max_seqs = progress.max_seq

			status = "\rVideo fragments: {0}; Audio fragments: {1}; ".format(frags[DTYPE_VIDEO], frags[DTYPE_AUDIO])
			if debug:
				status += "Max sequence: {0}; ".format(max_seqs)
			
			status += "Total Downloaded: {0}{1}{2}".format(format_size(total_bytes), " "*5, "\b"*5)
			info.set_status(status)
		except queue.Empty:
			pass
		except KeyboardInterrupt:
			# Attempt to shutdown gracefully by stopping the download threads
			with info.lock:
				info.stopping = True
			print("\nKeyboard Interrupt, stopping download...")

			for t in threads:
				t.join()

			print()
			merge = False
			if merge_on_cancel == Action.ASK:
				merge = get_yes_no("\nDownload stopped prematurely. Would you like to merge the currently downloaded data?")
			elif merge_on_cancel == Action.DO:
				merge = True

			if merge:
				alive = False
			else:
				save_files = False
				if save_on_cancel == Action.ASK:
					save_files = get_yes_no("\nWould you like to save any created files?")
				elif save_on_cancel == Action.DO:
					save_files = True

				if save_files:
					try_move(afile, os.path.join(fdir, "{0}.ts".format(afile_name)))
					try_move(vfile, os.path.join(fdir, "{0}.ts".format(vfile_name)))
					try_move(thmbnl_file, os.path.join(fdir, thmbnl_file_name))
					try_move(desc_file, os.path.join(fdir, desc_file_name))

				tmpdir.cleanup()
				sys.exit(2)
		
		if not alive:
			break

	print("\nDownload finished")
	aonly = info.quality == VIDEO_LABEL_ITAGS["audio_only"]

	# Attempt to mux the video and audio files using ffmpeg
	if not aonly and frags[DTYPE_AUDIO] != frags[DTYPE_VIDEO]:
		logwarn("Mismatched number of video and audio fragments.")
		logwarn("The files should still be mergable but data might be missing somewhere.")

	new_afile = os.path.join(fdir, "{0}.ts".format(afile_name))
	new_vfile = os.path.join(fdir, "{0}.ts".format(vfile_name))
	new_thmbnail = os.path.join(fdir, thmbnl_file_name)
	new_desc = os.path.join(fdir, desc_file_name)

	try_move(afile, new_afile)
	try_move(vfile, new_vfile)
	try_move(thmbnl_file, new_thmbnail)
	try_move(desc_file, new_desc)

	files.append(new_afile)
	files.append(new_vfile)
	if not write_thumb:
		files.append(new_thmbnail)

	tmpdir.cleanup()

	retcode = 0
	mfile = ""
	ffmpeg_args = [
		"ffmpeg",
		"-hide_banner",
		"-loglevel", "warning",
		"-i", new_afile
	]

	if thumbnail:
		ffmpeg_args.extend(["-i", new_thmbnail])

	if aonly:
		print("Correcting audio container")
		mfile = os.path.join(fdir, "{0}.m4a".format(fname))
	else:
		print("Muxing files")
		mfile = os.path.join(fdir, "{0}.mp4".format(fname))

		ffmpeg_args.extend([
			"-i", new_vfile,
			"-movflags", "faststart"
			])

		if thumbnail:
			ffmpeg_args.extend([
				"-map", "0",
				"-map", "1",
				"-map", "2"
			])
		
	ffmpeg_args.extend(["-c", "copy"])
	if thumbnail:
		ffmpeg_args.extend(["-disposition:v:0", "attached_pic"])

	if add_meta:
		for k, v in info.metadata.get_meta().items():
			if v:
				ffmpeg_args.extend([
					"-metadata",
					"{0}={1}".format(k.upper(), v)
				])
	
	ffmpeg_args.append(mfile)

	ffmpeg = shutil.which("ffmpeg")
	if not ffmpeg:
		print("***COMMAND THAT WOULD HAVE BEEN RUN***\n")
		print(" ".join(shlex.quote(x) for x in ffmpeg_args))
		print("\nffmpeg not found. Please install ffmpeg, then run the above command to create the final file.")
			
		sys.exit(0)

	retcode = execute(ffmpeg_args)

	if retcode != 0:
		print("execute returned code {0}. Something must have gone wrong with ffmpeg.".format(retcode))
		print("The .ts files will not be deleted in case the final file is broken.")
		sys.exit(retcode)

	cleanup_files(files)
	print()
	print("Final file: {0}".format(mfile))

if __name__ == "__main__":
	main()
