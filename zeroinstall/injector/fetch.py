"""
Downloads feeds, keys, packages and icons.
"""

# Copyright (C) 2009, Thomas Leonard
# See the README file for details, or visit http://0install.net.

from zeroinstall import _, NeedDownload, logger
import os, sys

from zeroinstall import support
from zeroinstall.support import tasks, basedir, portable_rename
from zeroinstall.injector.namespaces import XMLNS_IFACE, config_site
from zeroinstall.injector import model
from zeroinstall.injector.model import DownloadSource, Recipe, SafeException, escape, DistributionSource
from zeroinstall.injector.iface_cache import PendingFeed, ReplayAttack
from zeroinstall.injector.handler import NoTrustedKeys
from zeroinstall.injector import download

def _escape_slashes(path):
	return path.replace('/', '%23')

def _get_feed_dir(feed):
	"""The algorithm from 0mirror."""
	if '#' in feed:
		raise SafeException(_("Invalid URL '%s'") % feed)
	scheme, rest = feed.split('://', 1)
	assert '/' in rest, "Missing / in %s" % feed
	domain, rest = rest.split('/', 1)
	for x in [scheme, domain, rest]:
		if not x or x.startswith('.'):
			raise SafeException(_("Invalid URL '%s'") % feed)
	return '/'.join(['feeds', scheme, domain, _escape_slashes(rest)])

class KeyInfoFetcher:
	"""Fetches information about a GPG key from a key-info server.
	See L{Fetcher.fetch_key_info} for details.
	@since: 0.42

	Example:

	>>> kf = KeyInfoFetcher(fetcher, 'https://server', fingerprint)
	>>> while True:
		print kf.info
		if kf.blocker is None: break
		print kf.status
		yield kf.blocker
	"""
	def __init__(self, fetcher, server, fingerprint):
		self.fingerprint = fingerprint
		self.info = []
		self.blocker = None

		if server is None: return

		self.status = _('Fetching key information from %s...') % server

		dl = fetcher.download_url(server + '/key/' + fingerprint)

		from xml.dom import minidom

		@tasks.async
		def fetch_key_info():
			tempfile = dl.tempfile
			try:
				yield dl.downloaded
				self.blocker = None
				tasks.check(dl.downloaded)
				tempfile.seek(0)
				doc = minidom.parse(tempfile)
				if doc.documentElement.localName != 'key-lookup':
					raise SafeException(_('Expected <key-lookup>, not <%s>') % doc.documentElement.localName)
				self.info += doc.documentElement.childNodes
			except Exception as ex:
				doc = minidom.parseString('<item vote="bad"/>')
				root = doc.documentElement
				root.appendChild(doc.createTextNode(_('Error getting key information: %s') % ex))
				self.info.append(root)
			finally:
				tempfile.close()

		self.blocker = fetch_key_info()

class Fetcher(object):
	"""Downloads and stores various things.
	@ivar config: used to get handler, iface_cache and stores
	@type config: L{config.Config}
	@ivar key_info: caches information about GPG keys
	@type key_info: {str: L{KeyInfoFetcher}}
	"""
	__slots__ = ['config', 'key_info', '_scheduler', 'external_store']

	def __init__(self, config):
		assert config.handler, "API change!"
		self.config = config
		self.key_info = {}
		self._scheduler = None
		self.external_store = os.environ.get('ZEROINSTALL_EXTERNAL_STORE')

	@property
	def handler(self):
		return self.config.handler

	@property
	def scheduler(self):
		if self._scheduler is None:
			from . import scheduler
			self._scheduler = scheduler.DownloadScheduler()
		return self._scheduler

	# (force is deprecated and ignored)
	@tasks.async
	def cook(self, required_digest, recipe, stores, force = False, impl_hint = None):
		"""Follow a Recipe.
		@param impl_hint: the Implementation this is for (if any) as a hint for the GUI
		@see: L{download_impl} uses this method when appropriate"""
		# Maybe we're taking this metaphor too far?

		# Start a download for each ingredient
		blockers = []
		steps = []
		try:
			for stepdata in recipe.steps:
				cls = StepRunner.class_for(stepdata)
				step = cls(stepdata, impl_hint=impl_hint)
				step.prepare(self, blockers)
				steps.append(step)

			while blockers:
				yield blockers
				tasks.check(blockers)
				blockers = [b for b in blockers if not b.happened]


			if self.external_store:
				# Note: external_store will not yet work with non-<archive> steps.
				streams = [step.stream for step in steps]
				self._add_to_external_store(required_digest, recipe.steps, streams)
			else:
				# Create an empty directory for the new implementation
				store = stores.stores[0]
				tmpdir = store.get_tmp_dir_for(required_digest)
				try:
					# Unpack each of the downloaded archives into it in turn
					for step in steps:
						step.apply(tmpdir)
					# Check that the result is correct and store it in the cache
					store.check_manifest_and_rename(required_digest, tmpdir)
					tmpdir = None
				finally:
					# If unpacking fails, remove the temporary directory
					if tmpdir is not None:
						support.ro_rmtree(tmpdir)
		finally:
			for step in steps:
				try:
					step.close()
				except IOError as ex:
					# Can get "close() called during
					# concurrent operation on the same file
					# object." if we're unlucky (Python problem).
					logger.info("Failed to close: %s", ex)

	def _get_mirror_url(self, feed_url, resource):
		"""Return the URL of a mirror for this feed."""
		if self.config.mirror is None:
			return None
		if support.urlparse(feed_url).hostname == 'localhost':
			return None
		return '%s/%s/%s' % (self.config.mirror, _get_feed_dir(feed_url), resource)

	def get_feed_mirror(self, url):
		"""Return the URL of a mirror for this feed."""
		return self._get_mirror_url(url, 'latest.xml')

	def _get_archive_mirror(self, source):
		if self.config.mirror is None:
			return None
		if support.urlparse(source.url).hostname == 'localhost':
			return None
		if sys.version_info[0] > 2:
			from urllib.parse import quote
		else:
			from urllib import quote
		return '{mirror}/archive/{archive}'.format(
				mirror = self.config.mirror,
				archive = quote(source.url.replace('/', '#'), safe = ''))

	def _get_impl_mirror(self, impl):
		return self._get_mirror_url(impl.feed.url, 'impl/' + _escape_slashes(impl.id))

	@tasks.async
	def get_packagekit_feed(self, feed_url):
		"""Send a query to PackageKit (if available) for information about this package.
		On success, the result is added to iface_cache.
		"""
		assert feed_url.startswith('distribution:'), feed_url
		master_feed = self.config.iface_cache.get_feed(feed_url.split(':', 1)[1])
		if master_feed:
			fetch = self.config.iface_cache.distro.fetch_candidates(master_feed)
			if fetch:
				yield fetch
				tasks.check(fetch)

			# Force feed to be regenerated with the new information
			self.config.iface_cache.get_feed(feed_url, force = True)

	def download_and_import_feed(self, feed_url, iface_cache = None):
		"""Download the feed, download any required keys, confirm trust if needed and import.
		@param feed_url: the feed to be downloaded
		@type feed_url: str
		@param iface_cache: (deprecated)"""
		from .download import DownloadAborted

		assert iface_cache is None or iface_cache is self.config.iface_cache

		self.config.iface_cache.mark_as_checking(feed_url)
		
		logger.debug(_("download_and_import_feed %(url)s"), {'url': feed_url})
		assert not os.path.isabs(feed_url)

		if feed_url.startswith('distribution:'):
			return self.get_packagekit_feed(feed_url)

		primary = self._download_and_import_feed(feed_url, use_mirror = False)

		@tasks.named_async("monitor feed downloads for " + feed_url)
		def wait_for_downloads(primary):
			# Download just the upstream feed, unless it takes too long...
			timeout = tasks.TimeoutBlocker(5, 'Mirror timeout')		# 5 seconds

			yield primary, timeout
			tasks.check(timeout)

			try:
				tasks.check(primary)
				if primary.happened:
					return		# OK, primary succeeded!
				# OK, maybe it's just being slow...
				logger.info("Feed download from %s is taking a long time.", feed_url)
				primary_ex = None
			except NoTrustedKeys as ex:
				raise			# Don't bother trying the mirror if we have a trust problem
			except ReplayAttack as ex:
				raise			# Don't bother trying the mirror if we have a replay attack
			except DownloadAborted as ex:
				raise			# Don't bother trying the mirror if the user cancelled
			except SafeException as ex:
				# Primary failed
				primary = None
				primary_ex = ex
				logger.warn(_("Feed download from %(url)s failed: %(exception)s"), {'url': feed_url, 'exception': ex})

			# Start downloading from mirror...
			mirror = self._download_and_import_feed(feed_url, use_mirror = True)

			# Wait until both mirror and primary tasks are complete...
			while True:
				blockers = list(filter(None, [primary, mirror]))
				if not blockers:
					break
				yield blockers

				if primary:
					try:
						tasks.check(primary)
						if primary.happened:
							primary = None
							# No point carrying on with the mirror once the primary has succeeded
							if mirror:
								logger.info(_("Primary feed download succeeded; aborting mirror download for %s") % feed_url)
								mirror.dl.abort()
					except SafeException as ex:
						primary = None
						primary_ex = ex
						logger.info(_("Feed download from %(url)s failed; still trying mirror: %(exception)s"), {'url': feed_url, 'exception': ex})

				if mirror:
					try:
						tasks.check(mirror)
						if mirror.happened:
							mirror = None
							if primary_ex:
								# We already warned; no need to raise an exception too,
								# as the mirror download succeeded.
								primary_ex = None
					except ReplayAttack as ex:
						logger.info(_("Version from mirror is older than cached version; ignoring it: %s"), ex)
						mirror = None
						primary_ex = None
					except SafeException as ex:
						logger.info(_("Mirror download failed: %s"), ex)
						mirror = None

			if primary_ex:
				raise primary_ex

		return wait_for_downloads(primary)

	def _download_and_import_feed(self, feed_url, use_mirror):
		"""Download and import a feed.
		@param use_mirror: False to use primary location; True to use mirror."""
		if use_mirror:
			url = self.get_feed_mirror(feed_url)
			if url is None: return None
			logger.info(_("Trying mirror server for feed %s") % feed_url)
		else:
			url = feed_url

		dl = self.download_url(url, hint = feed_url)
		stream = dl.tempfile

		@tasks.named_async("fetch_feed " + url)
		def fetch_feed():
			try:
				yield dl.downloaded
				tasks.check(dl.downloaded)

				pending = PendingFeed(feed_url, stream)

				if use_mirror:
					# If we got the feed from a mirror, get the key from there too
					key_mirror = self.config.mirror + '/keys/'
				else:
					key_mirror = None

				keys_downloaded = tasks.Task(pending.download_keys(self, feed_hint = feed_url, key_mirror = key_mirror), _("download keys for %s") % feed_url)
				yield keys_downloaded.finished
				tasks.check(keys_downloaded.finished)

				if not self.config.iface_cache.update_feed_if_trusted(pending.url, pending.sigs, pending.new_xml):
					blocker = self.config.trust_mgr.confirm_keys(pending)
					if blocker:
						yield blocker
						tasks.check(blocker)
					if not self.config.iface_cache.update_feed_if_trusted(pending.url, pending.sigs, pending.new_xml):
						raise NoTrustedKeys(_("No signing keys trusted; not importing"))
			finally:
				stream.close()

		task = fetch_feed()
		task.dl = dl
		return task

	def fetch_key_info(self, fingerprint):
		try:
			return self.key_info[fingerprint]
		except KeyError:
			self.key_info[fingerprint] = key_info = KeyInfoFetcher(self,
									self.config.key_info_server, fingerprint)
			return key_info

	# (force is deprecated and ignored)
	def download_impl(self, impl, retrieval_method, stores, force = False):
		"""Download an implementation.
		@param impl: the selected implementation
		@type impl: L{model.ZeroInstallImplementation}
		@param retrieval_method: a way of getting the implementation (e.g. an Archive or a Recipe)
		@type retrieval_method: L{model.RetrievalMethod}
		@param stores: where to store the downloaded implementation
		@type stores: L{zerostore.Stores}
		@rtype: L{tasks.Blocker}"""
		assert impl
		assert retrieval_method

		if isinstance(retrieval_method, DistributionSource):
			return retrieval_method.install(self.handler)

		from zeroinstall.zerostore import manifest, parse_algorithm_digest_pair
		best = None
		for digest in impl.digests:
			alg_name, digest_value = parse_algorithm_digest_pair(digest)
			alg = manifest.algorithms.get(alg_name, None)
			if alg and (best is None or best.rating < alg.rating):
				best = alg
				required_digest = digest

		if best is None:
			if not impl.digests:
				raise SafeException(_("No <manifest-digest> given for '%(implementation)s' version %(version)s") %
						{'implementation': impl.feed.get_name(), 'version': impl.get_version()})
			raise SafeException(_("Unknown digest algorithms '%(algorithms)s' for '%(implementation)s' version %(version)s") %
					{'algorithms': impl.digests, 'implementation': impl.feed.get_name(), 'version': impl.get_version()})

		@tasks.async
		def download_impl(method):
			original_exception = None
			while True:
				try:
					if isinstance(method, DownloadSource):
						blocker, stream = self.download_archive(method, impl_hint = impl,
									may_use_mirror = original_exception is None)
						try:
							yield blocker
							tasks.check(blocker)

							stream.seek(0)
							if self.external_store:
								self._add_to_external_store(required_digest, [method], [stream])
							else:
								self._add_to_cache(required_digest, stores, method, stream)
						finally:
							stream.close()
					elif isinstance(method, Recipe):
						blocker = self.cook(required_digest, method, stores, impl_hint = impl)
						yield blocker
						tasks.check(blocker)
					else:
						raise Exception(_("Unknown download type for '%s'") % method)
				except download.DownloadError as ex:
					if original_exception:
						logger.info("Error from mirror: %s", ex)
						raise original_exception
					else:
						original_exception = ex
					mirror_url = self._get_impl_mirror(impl)
					if mirror_url is not None:
						logger.info("%s: trying implementation mirror at %s", ex, mirror_url)
						method = model.DownloadSource(impl, mirror_url,
									None, None, type = 'application/x-bzip-compressed-tar')
						continue		# Retry
					raise
				break

			self.handler.impl_added_to_store(impl)
		return download_impl(retrieval_method)

	def _add_to_cache(self, required_digest, stores, retrieval_method, stream):
		assert isinstance(retrieval_method, DownloadSource)
		stores.add_archive_to_cache(required_digest, stream, retrieval_method.url, retrieval_method.extract,
						 type = retrieval_method.type, start_offset = retrieval_method.start_offset or 0)

	def _add_to_external_store(self, required_digest, steps, streams):
		from zeroinstall.zerostore.unpack import type_from_url

		# combine archive path, extract directory and MIME type arguments in an alternating fashion
		paths = map(lambda stream: stream.name, streams)
		extracts = map(lambda step: step.extract or "", steps)
		types = map(lambda step: step.type or type_from_url(step.url), steps)
		args = [None]*(len(paths)+len(extracts)+len(types))
		args[::3] = paths
		args[1::3] = extracts
		args[2::3] = types

		# close file handles to allow external processes access
		for stream in streams:
			stream.close()

		# delegate extracting archives to external tool
		import subprocess
		subprocess.call([self.external_store, "add", required_digest] + args)

		# delete temp files
		for path in paths:
			os.remove(path)

	# (force is deprecated and ignored)
	def download_archive(self, download_source, force = False, impl_hint = None, may_use_mirror = False):
		"""Fetch an archive. You should normally call L{download_impl}
		instead, since it handles other kinds of retrieval method too.
		It is the caller's responsibility to ensure that the returned stream is closed.
		"""
		from zeroinstall.zerostore import unpack

		url = download_source.url
		if not (url.startswith('http:') or url.startswith('https:') or url.startswith('ftp:')):
			raise SafeException(_("Unknown scheme in download URL '%s'") % url)

		mime_type = download_source.type
		if not mime_type:
			mime_type = unpack.type_from_url(download_source.url)
		if not mime_type:
			raise SafeException(_("No 'type' attribute on archive, and I can't guess from the name (%s)") % download_source.url)
		if not self.external_store:
			unpack.check_type_ok(mime_type)

		if may_use_mirror:
			mirror = self._get_archive_mirror(download_source)
		else:
			mirror = None

		dl = self.download_url(download_source.url, hint = impl_hint, mirror_url = mirror)
		if download_source.size is not None:
			dl.expected_size = download_source.size + (download_source.start_offset or 0)
		# (else don't know sizes for mirrored archives)
		return (dl.downloaded, dl.tempfile)

	# (force is deprecated and ignored)
	def download_icon(self, interface, force = False):
		"""Download an icon for this interface and add it to the
		icon cache. If the interface has no icon do nothing.
		@return: the task doing the import, or None
		@rtype: L{tasks.Task}"""
		logger.debug("download_icon %(interface)s", {'interface': interface})

		modification_time = None
		existing_icon = self.config.iface_cache.get_icon_path(interface)
		if existing_icon:
			file_mtime = os.stat(existing_icon).st_mtime
			from email.utils import formatdate
			modification_time = formatdate(timeval = file_mtime, localtime = False, usegmt = True)

		feed = self.config.iface_cache.get_feed(interface.uri)
		if feed is None:
			return None

		# Find a suitable icon to download
		for icon in feed.get_metadata(XMLNS_IFACE, 'icon'):
			type = icon.getAttribute('type')
			if type != 'image/png':
				logger.debug(_('Skipping non-PNG icon'))
				continue
			source = icon.getAttribute('href')
			if source:
				break
			logger.warn(_('Missing "href" attribute on <icon> in %s'), interface)
		else:
			logger.info(_('No PNG icons found in %s'), interface)
			return

		dl = self.download_url(source, hint = interface, modification_time = modification_time)

		@tasks.async
		def download_and_add_icon():
			stream = dl.tempfile
			try:
				yield dl.downloaded
				tasks.check(dl.downloaded)
				if dl.unmodified: return
				stream.seek(0)

				import shutil, tempfile
				icons_cache = basedir.save_cache_path(config_site, 'interface_icons')

				tmp_file = tempfile.NamedTemporaryFile(dir = icons_cache, delete = False)
				shutil.copyfileobj(stream, tmp_file)
				tmp_file.close()

				icon_file = os.path.join(icons_cache, escape(interface.uri))
				portable_rename(tmp_file.name, icon_file)
			finally:
				stream.close()

		return download_and_add_icon()

	def download_impls(self, implementations, stores):
		"""Download the given implementations, choosing a suitable retrieval method for each.
		If any of the retrieval methods are DistributionSources and
		need confirmation, handler.confirm is called to check that the
		installation should proceed.
		"""
		unsafe_impls = []

		to_download = []
		for impl in implementations:
			logger.debug(_("start_downloading_impls: for %(feed)s get %(implementation)s"), {'feed': impl.feed, 'implementation': impl})
			source = self.get_best_source(impl)
			if not source:
				raise SafeException(_("Implementation %(implementation_id)s of interface %(interface)s"
					" cannot be downloaded (no download locations given in "
					"interface!)") % {'implementation_id': impl.id, 'interface': impl.feed.get_name()})
			to_download.append((impl, source))

			if isinstance(source, DistributionSource) and source.needs_confirmation:
				unsafe_impls.append(source.package_id)

		@tasks.async
		def download_impls():
			if unsafe_impls:
				confirm = self.handler.confirm_install(_('The following components need to be installed using native packages. '
					'These come from your distribution, and should therefore be trustworthy, but they also '
					'run with extra privileges. In particular, installing them may run extra services on your '
					'computer or affect other users. You may be asked to enter a password to confirm. The '
					'packages are:\n\n') + ('\n'.join('- ' + x for x in unsafe_impls)))
				yield confirm
				tasks.check(confirm)

			blockers = []

			for impl, source in to_download:
				blockers.append(self.download_impl(impl, source, stores))

			# Record the first error log the rest
			error = []
			def dl_error(ex, tb = None):
				if error:
					self.handler.report_error(ex)
				else:
					error.append((ex, tb))
			while blockers:
				yield blockers
				tasks.check(blockers, dl_error)

				blockers = [b for b in blockers if not b.happened]
			if error:
				from zeroinstall import support
				support.raise_with_traceback(*error[0])

		if not to_download:
			return None

		return download_impls()

	def get_best_source(self, impl):
		"""Return the best download source for this implementation.
		@rtype: L{model.RetrievalMethod}"""
		if impl.download_sources:
			return impl.download_sources[0]
		return None

	def download_url(self, url, hint = None, modification_time = None, expected_size = None, mirror_url = None):
		"""The most low-level method here; just download a raw URL.
		It is the caller's responsibility to ensure that dl.stream is closed.
		@param url: the location to download from
		@param hint: user-defined data to store on the Download (e.g. used by the GUI)
		@param modification_time: don't download unless newer than this
		@param mirror_url: an altertive URL to try if this one fails
		@type mirror_url: str
		@rtype: L{download.Download}
		@since: 1.5
		"""
		if self.handler.dry_run:
			raise NeedDownload(url)

		dl = download.Download(url, hint = hint, modification_time = modification_time, expected_size = expected_size, auto_delete = not self.external_store)
		dl.mirror = mirror_url
		self.handler.monitor_download(dl)
		dl.downloaded = self.scheduler.download(dl)
		return dl

class StepRunner(object):
	"""The base class of all step runners.
	@since: 1.10"""

	def __init__(self, stepdata, impl_hint):
		self.stepdata = stepdata
		self.impl_hint = impl_hint

	def prepare(self, fetcher, blockers):
		pass

	@classmethod
	def class_for(cls, model):
		for subcls in cls.__subclasses__():
			if subcls.model_type == type(model):
				return subcls
		assert False, "Couldn't find step runner for %s" % (type(model),)
	
	def close(self):
		"""Release any resources (called on success or failure)."""
		pass

class RenameStepRunner(StepRunner):
	"""A step runner for the <rename> step.
	@since: 1.10"""

	model_type = model.RenameStep

	def apply(self, basedir):
		source = native_path_within_base(basedir, self.stepdata.source)
		dest = native_path_within_base(basedir, self.stepdata.dest)
		os.rename(source, dest)

class DownloadStepRunner(StepRunner):
	"""A step runner for the <archive> step.
	@since: 1.10"""

	model_type = model.DownloadSource

	def prepare(self, fetcher, blockers):
		self.blocker, self.stream = fetcher.download_archive(self.stepdata, impl_hint = self.impl_hint, may_use_mirror = True)
		assert self.stream
		blockers.append(self.blocker)
	
	def apply(self, basedir):
		from zeroinstall.zerostore import unpack
		assert self.blocker.happened
		unpack.unpack_archive_over(self.stepdata.url, self.stream, basedir,
				extract = self.stepdata.extract,
				type=self.stepdata.type,
				start_offset = self.stepdata.start_offset or 0)
	
	def close(self):
		self.stream.close()

def native_path_within_base(base, crossplatform_path):
	"""Takes a cross-platform relative path (i.e using forward slashes, even on windows)
	and returns the absolute, platform-native version of the path.
	If the path does not resolve to a location within `base`, a SafeError is raised.
	@since: 1.10
	"""
	assert os.path.isabs(base)
	if crossplatform_path.startswith("/"):
		raise SafeException("path %r is not within the base directory" % (crossplatform_path,))
	native_path = os.path.join(*crossplatform_path.split("/"))
	fullpath = os.path.realpath(os.path.join(base, native_path))
	base = os.path.realpath(base)
	if not fullpath.startswith(base + os.path.sep):
		raise SafeException("path %r is not within the base directory" % (crossplatform_path,))
	return fullpath
