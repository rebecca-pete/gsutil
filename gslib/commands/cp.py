# -*- coding: utf-8 -*-
# Copyright 2011 Google Inc. All Rights Reserved.
# Copyright 2011, Nexenta Systems Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Implementation of Unix-like cp command for cloud storage providers."""

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division
from __future__ import unicode_literals

import errno
import itertools
import logging
import os
import time
import traceback

from apitools.base.py import encoding
from gslib.command import Command
from gslib.command_argument import CommandArgument
from gslib.cs_api_map import ApiSelector
from gslib.exception import CommandException
from gslib.metrics import LogPerformanceSummaryParams
from gslib.name_expansion import CopyObjectsIterator
from gslib.name_expansion import DestinationInfo
from gslib.name_expansion import NameExpansionIterator
from gslib.name_expansion import NameExpansionIteratorDestinationTuple
from gslib.name_expansion import SeekAheadNameExpansionIterator
from gslib.storage_url import ContainsWildcard
from gslib.storage_url import IsCloudSubdirPlaceholder
from gslib.storage_url import StorageUrlFromString
from gslib.third_party.storage_apitools import storage_v1_messages as apitools_messages
from gslib.utils import cat_helper
from gslib.utils import copy_helper
from gslib.utils import parallelism_framework_util
from gslib.utils.cloud_api_helper import GetCloudApiInstance
from gslib.utils.constants import DEBUGLEVEL_DUMP_REQUESTS
from gslib.utils.constants import NO_MAX
from gslib.utils.copy_helper import CreateCopyHelperOpts
from gslib.utils.copy_helper import GetSourceFieldsNeededForCopy
from gslib.utils.copy_helper import GZIP_ALL_FILES
from gslib.utils.copy_helper import ItemExistsError
from gslib.utils.copy_helper import Manifest
from gslib.utils.copy_helper import SkipUnsupportedObjectError
from gslib.utils.posix_util import ConvertModeToBase8
from gslib.utils.posix_util import DeserializeFileAttributesFromObjectMetadata
from gslib.utils.posix_util import InitializeUserGroups
from gslib.utils.posix_util import POSIXAttributes
from gslib.utils.posix_util import SerializeFileAttributesToObjectMetadata
from gslib.utils.posix_util import ValidateFilePermissionAccess
from gslib.utils.shim_util import GcloudStorageFlag
from gslib.utils.shim_util import GcloudStorageMap
from gslib.utils.system_util import GetStreamFromFileUrl
from gslib.utils.system_util import StdinIterator
from gslib.utils.system_util import StdinIteratorCls
from gslib.utils.text_util import NormalizeStorageClass
from gslib.utils.text_util import RemoveCRLFFromString
from gslib.utils.unit_util import CalculateThroughput
from gslib.utils.unit_util import MakeHumanReadable

_SYNOPSIS = """
  gsutil cp [OPTION]... src_url dst_url
  gsutil cp [OPTION]... src_url... dst_url
  gsutil cp [OPTION]... -I dst_url
"""

_SYNOPSIS_TEXT = """
<B>SYNOPSIS</B>
""" + _SYNOPSIS

_DESCRIPTION_TEXT = """

  NOTE: For a list of permissions required for specific actions, see
 `IAM permissions for gsutil commands <https://cloud.google.com/storage/docs/access-control/iam-gsutil>`_.

  For a list of relevant roles, see
  `Cloud Storage roles <https://cloud.google.com/storage/docs/access-control/iam-roles>`_.
  Alternatively, you can
  `create a custom role <https://cloud.google.com/iam/docs/creating-custom-roles>`_
  that has specific, limited permissions.

<B>DESCRIPTION</B>
  The ``gsutil cp`` command allows you to copy data between your local file
  system and the cloud, within the cloud, and between
  cloud storage providers. For example, to upload all text files from the
  local directory to a bucket, you can run:

    gsutil cp *.txt gs://my-bucket

  You can also download data from a bucket. The following command downloads
  all text files from the top-level of a bucket to your current directory:

    gsutil cp gs://my-bucket/*.txt .

  You can use the ``-n`` option to prevent overwriting the content of
  existing files. The following example downloads text files from a bucket
  without clobbering the data in your directory:

    gsutil cp -n gs://my-bucket/*.txt .

  Use the ``-r`` option to copy an entire directory tree.
  For example, to upload the directory tree ``dir``:

    gsutil cp -r dir gs://my-bucket

  If you have a large number of files to transfer, you can perform a parallel
  multi-threaded/multi-processing copy using the
  top-level gsutil ``-m`` option (see "gsutil help options"):

    gsutil -m cp -r dir gs://my-bucket

  You can use the ``-I`` option with ``stdin`` to specify a list of URLs to
  copy, one per line. This allows you to use gsutil
  in a pipeline to upload or download objects as generated by a program:

    cat filelist | gsutil -m cp -I gs://my-bucket

  or:

    cat filelist | gsutil -m cp -I ./download_dir

  where the output of ``cat filelist`` is a list of files, cloud URLs, and
  wildcards of files and cloud URLs.

  NOTE: Shells like ``bash`` and ``zsh`` sometimes attempt to expand
  wildcards in ways that can be surprising. You may also encounter issues when
  attempting to copy files whose names contain wildcard characters. For more
  details about these issues, see "Potentially Surprising Behavior When Using Wildcards"
  under "gsutil help wildcards".
"""

_NAME_CONSTRUCTION_TEXT = """
<B>HOW NAMES ARE CONSTRUCTED</B>
  The ``gsutil cp`` command attempts to name objects in ways that are consistent with the
  Linux ``cp`` command. This means that names are constructed depending
  on whether you're performing a recursive directory copy or copying
  individually-named objects, or whether you're copying to an existing or
  non-existent directory.

  When you perform recursive directory copies, object names are constructed to
  mirror the source directory structure starting at the point of recursive
  processing. For example, if ``dir1/dir2`` contains the file ``a/b/c``, then the
  following command creates the object ``gs://my-bucket/dir2/a/b/c``:

    gsutil cp -r dir1/dir2 gs://my-bucket

  In contrast, copying individually-named files results in objects named by
  the final path component of the source files. For example, assuming again that
  ``dir1/dir2`` contains ``a/b/c``, the following command creates the object
  ``gs://my-bucket/c``:

    gsutil cp dir1/dir2/** gs://my-bucket

  Note that in the above example, the '**' wildcard matches all names
  anywhere under ``dir``. The wildcard '*' matches names just one level deep. For
  more details, see "gsutil help wildcards".

  The same rules apply for uploads and downloads: recursive copies of buckets and
  bucket subdirectories produce a mirrored filename structure, while copying
  individually or wildcard-named objects produce flatly-named files.

  In addition, the resulting names depend on whether the destination subdirectory
  exists. For example, if ``gs://my-bucket/subdir`` exists as a subdirectory,
  the following command creates the object ``gs://my-bucket/subdir/dir2/a/b/c``:

    gsutil cp -r dir1/dir2 gs://my-bucket/subdir

  In contrast, if ``gs://my-bucket/subdir`` does not exist, this same ``gsutil cp``
  command creates the object ``gs://my-bucket/subdir/a/b/c``.

  NOTE: The
  `Google Cloud Platform Console <https://console.cloud.google.com>`_
  creates folders by creating "placeholder" objects that end
  with a "/" character. gsutil skips these objects when downloading from the
  cloud to the local file system, because creating a file that
  ends with a "/" is not allowed on Linux and macOS. We
  recommend that you only create objects that end with "/" if you don't
  intend to download such objects using gsutil.
"""

_SUBDIRECTORIES_TEXT = """
<B>COPYING TO/FROM SUBDIRECTORIES; DISTRIBUTING TRANSFERS ACROSS MACHINES</B>
  You can use gsutil to copy to and from subdirectories by using a command
  like this:

    gsutil cp -r dir gs://my-bucket/data

  This causes ``dir`` and all of its files and nested subdirectories to be
  copied under the specified destination, resulting in objects with names like
  ``gs://my-bucket/data/dir/a/b/c``. Similarly, you can download from bucket
  subdirectories using the following command:

    gsutil cp -r gs://my-bucket/data dir

  This causes everything nested under ``gs://my-bucket/data`` to be downloaded
  into ``dir``, resulting in files with names like ``dir/data/a/b/c``.

  Copying subdirectories is useful if you want to add data to an existing
  bucket directory structure over time. It's also useful if you want
  to parallelize uploads and downloads across multiple machines (potentially
  reducing overall transfer time compared with running ``gsutil -m
  cp`` on one machine). For example, if your bucket contains this structure:

    gs://my-bucket/data/result_set_01/
    gs://my-bucket/data/result_set_02/
    ...
    gs://my-bucket/data/result_set_99/

  you can perform concurrent downloads across 3 machines by running these
  commands on each machine, respectively:

    gsutil -m cp -r gs://my-bucket/data/result_set_[0-3]* dir
    gsutil -m cp -r gs://my-bucket/data/result_set_[4-6]* dir
    gsutil -m cp -r gs://my-bucket/data/result_set_[7-9]* dir

  Note that ``dir`` could be a local directory on each machine, or a
  directory mounted off of a shared file server. The performance of the latter
  depends on several factors, so we recommend experimenting
  to find out what works best for your computing environment.
"""

_COPY_IN_CLOUD_TEXT = """
<B>COPYING IN THE CLOUD AND METADATA PRESERVATION</B>
  If both the source and destination URL are cloud URLs from the same
  provider, gsutil copies data "in the cloud" (without downloading
  to and uploading from the machine where you run gsutil). In addition to
  the performance and cost advantages of doing this, copying in the cloud
  preserves metadata such as ``Content-Type`` and ``Cache-Control``. In contrast,
  when you download data from the cloud, it ends up in a file with
  no associated metadata, unless you have some way to keep
  or re-create that metadata.

  Copies spanning locations and/or storage classes cause data to be rewritten
  in the cloud, which may take some time (but is still faster than
  downloading and re-uploading). Such operations can be resumed with the same
  command if they are interrupted, so long as the command parameters are
  identical.

  Note that by default, the gsutil ``cp`` command does not copy the object
  ACL to the new object, and instead uses the default bucket ACL (see
  "gsutil help defacl"). You can override this behavior with the ``-p``
  option.

  When copying in the cloud, if the destination bucket has Object Versioning
  enabled, by default ``gsutil cp`` copies only live versions of the
  source object. For example, the following command causes only the single live
  version of ``gs://bucket1/obj`` to be copied to ``gs://bucket2``, even if there
  are noncurrent versions of ``gs://bucket1/obj``:

    gsutil cp gs://bucket1/obj gs://bucket2

   To also copy noncurrent versions, use the ``-A`` flag:

    gsutil cp -A gs://bucket1/obj gs://bucket2

  The top-level gsutil ``-m`` flag is  not allowed when using the ``cp -A`` flag.
"""

_CHECKSUM_VALIDATION_TEXT = """


<B>CHECKSUM VALIDATION</B>
  At the end of every upload or download, the ``gsutil cp`` command validates that
  the checksum it computes for the source file matches the checksum that
  the service computes. If the checksums do not match, gsutil deletes the
  corrupted object and prints a warning message. If this happens, contact
  gs-team@google.com.

  If you know the MD5 of a file before uploading, you can specify it in the
  Content-MD5 header, which enables the cloud storage service to reject the
  upload if the MD5 doesn't match the value computed by the service. For
  example:

    % gsutil hash obj
    Hashing     obj:
    Hashes [base64] for obj:
            Hash (crc32c):          lIMoIw==
            Hash (md5):             VgyllJgiiaRAbyUUIqDMmw==

    % gsutil -h Content-MD5:VgyllJgiiaRAbyUUIqDMmw== cp obj gs://your-bucket/obj
    Copying file://obj [Content-Type=text/plain]...
    Uploading   gs://your-bucket/obj:                                182 b/182 B

    If the checksums don't match, the service rejects the upload and
    gsutil prints a message like:

    BadRequestException: 400 Provided MD5 hash "VgyllJgiiaRAbyUUIqDMmw=="
    doesn't match calculated MD5 hash "7gyllJgiiaRAbyUUIqDMmw==".

  Specifying the Content-MD5 header has several advantages:

  1. It prevents the corrupted object from becoming visible. If you don't
     specify the header, the object is visible for 1-3 seconds before gsutil deletes
     it.

  2. If an object already exists with the given name, specifying the
     Content-MD5 header prevents the existing object from being replaced.
     Otherwise, the existing object is replaced by the corrupted object and
     deleted a few seconds later.

  3. If you don't specify the Content-MD5 header, it's possible for the gsutil
     process to complete the upload but then be interrupted or fail before it can
     delete the corrupted object, leaving the corrupted object in the cloud.

  4. It supports a customer-to-service integrity check handoff. For example,
     if you have a content production pipeline that generates data to be
     uploaded to the cloud along with checksums of that data, specifying the
     MD5 computed by your content pipeline when you run ``gsutil cp`` ensures
     that the checksums match all the way through the process. This way, you can
     detect if data gets corrupted on your local disk between the time it was written
     by your content pipeline and the time it was uploaded to Google Cloud
     Storage.

  NOTE: The Content-MD5 header is ignored for composite objects, which only have
  a CRC32C checksum.
"""

_RETRY_HANDLING_TEXT = """
<B>RETRY HANDLING</B>
  The ``cp`` command retries when failures occur, but if enough failures happen
  during a particular copy or delete operation, or if a failure isn't retryable,
  the ``cp`` command skips that object and moves on. If any failures were not
  successfully retried by the end of the copy run, the ``cp`` command reports the
  number of failures, and exits with a non-zero status.

  For details about gsutil's overall retry handling, see `Retry strategy
  <https://cloud.google.com/storage/docs/retry-strategy#tools>`_.
"""

_RESUMABLE_TRANSFERS_TEXT = """
<B>RESUMABLE TRANSFERS</B>
  gsutil automatically resumes interrupted downloads and interrupted `resumable
  uploads <https://cloud.google.com/storage/docs/resumable-uploads#gsutil>`_,
  except when performing streaming transfers. In the case of an interrupted
  download, a partially downloaded temporary file is visible in the destination
  directory. Upon completion, the original file is deleted and replaced with the
  downloaded contents.

  Resumable transfers store state information in files under
  ~/.gsutil, named by the destination object or file.

  See "gsutil help prod" for details on using resumable transfers
  in production.
"""

_STREAMING_TRANSFERS_TEXT = """
<B>STREAMING TRANSFERS</B>
  Use '-' in place of src_url or dst_url to perform a `streaming transfer
  <https://cloud.google.com/storage/docs/streaming>`_.

  Streaming uploads using the `JSON API
  <https://cloud.google.com/storage/docs/request-endpoints#gsutil>`_ are buffered
  in memory part-way back into the file and can thus sometimes resume in the event
  of network or service problems.

  gsutil does not support resuming streaming uploads using the XML API or
  resuming streaming downloads for either JSON or XML. If you have a large amount
  of data to transfer in these cases, we recommend that you write the data to a
  local file and copy that file rather than streaming it.
"""

_SLICED_OBJECT_DOWNLOADS_TEXT = """
<B>SLICED OBJECT DOWNLOADS</B>
  gsutil uses HTTP Range GET requests to perform "sliced" downloads in parallel
  when downloading large objects from Cloud Storage. This means that disk
  space for the temporary download destination file is pre-allocated and
  byte ranges (slices) within the file are downloaded in parallel. Once all
  slices have completed downloading, the temporary file is renamed to the
  destination file. No additional local disk space is required for this
  operation.

  This feature is only available for Cloud Storage objects because it
  requires a fast composable checksum (CRC32C) to verify the
  data integrity of the slices. Because sliced object downloads depend on CRC32C,
  they require a compiled crcmod on the machine performing the download. If compiled
  crcmod is not available, a non-sliced object download is performed instead.

  NOTE: Since sliced object downloads cause multiple writes to occur at various
  locations on disk, this mechanism can degrade performance for disks with slow
  seek times, especially for large numbers of slices. While the default number
  of slices is set small to avoid this problem, you can disable sliced object
  download if necessary by setting the "sliced_object_download_threshold"
  variable in the ``.boto`` config file to 0.
"""

_PARALLEL_COMPOSITE_UPLOADS_TEXT = """
<B>PARALLEL COMPOSITE UPLOADS</B>
  gsutil can automatically use
  `object composition <https://cloud.google.com/storage/docs/composite-objects>`_
  to perform uploads in parallel for large, local files being uploaded to
  Cloud Storage. See the `Uploads and downloads documentation
  <https://cloud.google.com/storage/docs/uploads-downloads#parallel-composite-uploads>`_
  for a complete discussion.
"""

_CHANGING_TEMP_DIRECTORIES_TEXT = """
<B>CHANGING TEMP DIRECTORIES</B>
  gsutil writes data to a temporary directory in several cases:

  - when compressing data to be uploaded (see the ``-z`` and ``-Z`` options)
  - when decompressing data being downloaded (for example, when the data has
    ``Content-Encoding:gzip`` as a result of being uploaded
    using gsutil cp -z or gsutil cp -Z)
  - when running integration tests using the gsutil test command

  In these cases, it's possible the temporary file location on your system that
  gsutil selects by default may not have enough space. If gsutil runs out of
  space during one of these operations (for example, raising
  "CommandException: Inadequate temp space available to compress <your file>"
  during a ``gsutil cp -z`` operation), you can change where it writes these
  temp files by setting the TMPDIR environment variable. On Linux and macOS,
  you can set the variable as follows:

    TMPDIR=/some/directory gsutil cp ...

  You can also add this line to your ~/.bashrc file and restart the shell
  before running gsutil:

    export TMPDIR=/some/directory

  On Windows 7, you can change the TMPDIR environment variable from Start ->
  Computer -> System -> Advanced System Settings -> Environment Variables.
  You need to reboot after making this change for it to take effect. Rebooting
  is not necessary after running the export command on Linux and macOS.
"""

_COPYING_SPECIAL_FILES_TEXT = """
<B>SYNCHRONIZING OVER OS-SPECIFIC FILE TYPES (SUCH AS SYMLINKS AND DEVICES)</B>

  Please see the section about OS-specific file types in "gsutil help rsync".
  While that section refers to the ``rsync`` command, analogous
  points apply to the ``cp`` command.
"""

_OPTIONS_TEXT = """
<B>OPTIONS</B>
  -a canned_acl  Applies the specific ``canned_acl`` to uploaded objects. See
                 "gsutil help acls" for further details.

  -A             Copy all source versions from a source bucket or folder.
                 If not set, only the live version of each source object is
                 copied.

                 NOTE: This option is only useful when the destination
                 bucket has Object Versioning enabled. Additionally, the generation
                 numbers of copied versions do not necessarily match the order of the
                 original generation numbers.

  -c             If an error occurs, continue attempting to copy the remaining
                 files. If any copies are unsuccessful, gsutil's exit status
                 is non-zero, even if this flag is set. This option is
                 implicitly set when running ``gsutil -m cp...``.

                 NOTE: ``-c`` only applies to the actual copying operation. If an
                 error, such as ``invalid Unicode file name``, occurs while iterating
                 over the files in the local directory, gsutil prints an error
                 message and aborts.

  -D             Copy in "daisy chain" mode, which means copying between two buckets
                 by first downloading to the machine where gsutil is run, then
                 uploading to the destination bucket. The default mode is a
                 "copy in the cloud," where data is copied between two buckets without
                 uploading or downloading.

                 During a "copy in the cloud," a source composite object remains composite
                 at its destination. However, you can use "daisy chain" mode to change a
                 composite object into a non-composite object. For example:

                     gsutil cp -D gs://bucket/obj gs://bucket/obj_tmp
                     gsutil mv gs://bucket/obj_tmp gs://bucket/obj

                 NOTE: "Daisy chain" mode is automatically used when copying
                 between providers: for example, when copying data from Cloud Storage
                 to another provider.

  -e             Exclude symlinks. When specified, symbolic links are not copied.

  -I             Use ``stdin`` to specify a list of files or objects to copy. You can use
                 gsutil in a pipeline to upload or download objects as generated by a program.
                 For example:

                   cat filelist | gsutil -m cp -I gs://my-bucket

                 where the output of ``cat filelist`` is a one-per-line list of
                 files, cloud URLs, and wildcards of files and cloud URLs.

  -j <ext,...>   Applies gzip transport encoding to any file upload whose
                 extension matches the ``-j`` extension list. This is useful when
                 uploading files with compressible content such as .js, .css,
                 or .html files. This also saves network bandwidth while
                 leaving the data uncompressed in Cloud Storage.

                 When you specify the ``-j`` option, files being uploaded are
                 compressed in-memory and on-the-wire only. Both the local
                 files and Cloud Storage objects remain uncompressed. The
                 uploaded objects retain the ``Content-Type`` and name of the
                 original files.

                 Note that if you want to use the top-level ``-m`` option to
                 parallelize copies along with the ``-j/-J`` options, your
                 performance may be bottlenecked by the
                 "max_upload_compression_buffer_size" boto config option,
                 which is set to 2 GiB by default. You can change this
                 compression buffer size to a higher limit. For example:

                   gsutil -o "GSUtil:max_upload_compression_buffer_size=8G" \\
                     -m cp -j html,txt -r /local/source/dir gs://bucket/path

  -J             Applies gzip transport encoding to file uploads. This option
                 works like the ``-j`` option described above, but it applies to
                 all uploaded files, regardless of extension.

                 CAUTION: If some of the source files don't compress well, such
                 as binary data, using this option may result in longer uploads.

  -L <file>      Outputs a manifest log file with detailed information about
                 each item that was copied. This manifest contains the following
                 information for each item:

                 - Source path.
                 - Destination path.
                 - Source size.
                 - Bytes transferred.
                 - MD5 hash.
                 - Transfer start time and date in UTC and ISO 8601 format.
                 - Transfer completion time and date in UTC and ISO 8601 format.
                 - Upload id, if a resumable upload was performed.
                 - Final result of the attempted transfer, either success or failure.
                 - Failure details, if any.

                 If the log file already exists, gsutil uses the file as an
                 input to the copy process, and appends log items to
                 the existing file. Objects that are marked in the
                 existing log file as having been successfully copied or
                 skipped are ignored. Objects without entries are
                 copied and ones previously marked as unsuccessful are
                 retried. This option can be used in conjunction with the ``-c`` option to
                 build a script that copies a large number of objects reliably,
                 using a bash script like the following:

                   until gsutil cp -c -L cp.log -r ./dir gs://bucket; do
                     sleep 1
                   done

                 The -c option enables copying to continue after failures
                 occur, and the -L option allows gsutil to pick up where it
                 left off without duplicating work. The loop continues
                 running as long as gsutil exits with a non-zero status. A non-zero
                 status indicates there was at least one failure during the copy
                 operation.

                 NOTE: If you are synchronizing the contents of a
                 directory and a bucket, or the contents of two buckets, see
                 "gsutil help rsync".

  -n             No-clobber. When specified, existing files or objects at the
                 destination are not replaced. Any items that are skipped
                 by this option are reported as skipped. gsutil
                 performs an additional GET request to check if an item
                 exists before attempting to upload the data. This saves gsutil
                 from retransmitting data, but the additional HTTP requests may make
                 small object transfers slower and more expensive.

  -p             Preserves ACLs when copying in the cloud. Note
                 that this option has performance and cost implications only when
                 using the XML API, as the XML API requires separate HTTP calls for
                 interacting with ACLs. You can mitigate this
                 performance issue using ``gsutil -m cp`` to perform parallel
                 copying. Note that this option only works if you have OWNER access
                 to all objects that are copied. If you want all objects in the
                 destination bucket to end up with the same ACL, you can avoid these
                 performance issues by setting a default object ACL on that bucket
                 instead of using ``cp -p``. See "gsutil help defacl".

                 Note that it's not valid to specify both the ``-a`` and ``-p`` options
                 together.

  -P             Enables POSIX attributes to be preserved when objects are
                 copied. ``gsutil cp`` copies fields provided by ``stat``. These fields
                 are the user ID of the owner, the group
                 ID of the owning group, the mode or permissions of the file, and
                 the access and modification time of the file. For downloads, these
                 attributes are only set if the source objects were uploaded
                 with this flag enabled.

                 On Windows, this flag only sets and restores access time and
                 modification time. This is because Windows doesn't support
                 POSIX uid/gid/mode.

  -R, -r         The ``-R`` and ``-r`` options are synonymous. They enable directories,
                 buckets, and bucket subdirectories to be copied recursively.
                 If you don't use this option for an upload, gsutil copies objects
                 it finds and skips directories. Similarly, if you don't
                 specify this option for a download, gsutil copies
                 objects at the current bucket directory level and skips subdirectories.

  -s <class>     Specifies the storage class of the destination object. If not
                 specified, the default storage class of the destination bucket
                 is used. This option is not valid for copying to non-cloud destinations.

  -U             Skips objects with unsupported object types instead of failing.
                 Unsupported object types include Amazon S3 objects in the GLACIER
                 storage class.

  -v             Prints the version-specific URL for each uploaded object. You can
                 use these URLs to safely make concurrent upload requests, because
                 Cloud Storage refuses to perform an update if the current
                 object version doesn't match the version-specific URL. See
                 `generation numbers
                 <https://cloud.google.com/storage/docs/metadata#generation-number>`_
                 for more details.

  -z <ext,...>   Applies gzip content-encoding to any file upload whose
                 extension matches the ``-z`` extension list. This is useful when
                 uploading files with compressible content such as .js, .css,
                 or .html files, because it reduces network bandwidth and storage
                 sizes. This can both improve performance and reduce costs.

                 When you specify the ``-z`` option, the data from your files is
                 compressed before it is uploaded, but your actual files are
                 left uncompressed on the local disk. The uploaded objects
                 retain the ``Content-Type`` and name of the original files, but
                 have their ``Content-Encoding`` metadata set to ``gzip`` to
                 indicate that the object data stored are compressed on the
                 Cloud Storage servers and have their ``Cache-Control`` metadata
                 set to ``no-transform``.

                 For example, the following command:

                   gsutil cp -z html \\
                     cattypes.html tabby.jpeg gs://mycats

                 does the following:

                 - The ``cp`` command uploads the files ``cattypes.html`` and
                   ``tabby.jpeg`` to the bucket ``gs://mycats``.
                 - Based on the file extensions, gsutil sets the ``Content-Type``
                   of ``cattypes.html`` to ``text/html`` and ``tabby.jpeg`` to
                   ``image/jpeg``.
                 - The ``-z`` option compresses the data in the file ``cattypes.html``.
                 - The ``-z`` option also sets the ``Content-Encoding`` for
                   ``cattypes.html`` to ``gzip`` and the ``Cache-Control`` for
                   ``cattypes.html`` to ``no-transform``.

                 Because the ``-z/-Z`` options compress data prior to upload, they
                 are not subject to the same compression buffer bottleneck that
                 can affect the ``-j/-J`` options.

                 Note that if you download an object with ``Content-Encoding:gzip``,
                 gsutil decompresses the content before writing the local file.

  -Z             Applies gzip content-encoding to file uploads. This option
                 works like the ``-z`` option described above, but it applies to
                 all uploaded files, regardless of extension.

                 CAUTION: If some of the source files don't compress well, such
                 as binary data, using this option may result in files taking up
                 more space in the cloud than they would if left uncompressed.

  --stet         If the STET binary can be found in boto or PATH, cp will
                 use the split-trust encryption tool for end-to-end encryption.
"""

_DETAILED_HELP_TEXT = '\n\n'.join([
    _SYNOPSIS_TEXT,
    _DESCRIPTION_TEXT,
    _NAME_CONSTRUCTION_TEXT,
    _SUBDIRECTORIES_TEXT,
    _COPY_IN_CLOUD_TEXT,
    _CHECKSUM_VALIDATION_TEXT,
    _RETRY_HANDLING_TEXT,
    _RESUMABLE_TRANSFERS_TEXT,
    _STREAMING_TRANSFERS_TEXT,
    _SLICED_OBJECT_DOWNLOADS_TEXT,
    _PARALLEL_COMPOSITE_UPLOADS_TEXT,
    _CHANGING_TEMP_DIRECTORIES_TEXT,
    _COPYING_SPECIAL_FILES_TEXT,
    _OPTIONS_TEXT,
])

CP_SUB_ARGS = 'a:AcDeIL:MNnpPrRs:tUvz:Zj:J'


def _CopyFuncWrapper(cls, args, thread_state=None):
  cls.CopyFunc(args,
               thread_state=thread_state,
               preserve_posix=cls.preserve_posix_attrs)


def _CopyExceptionHandler(cls, e):
  """Simple exception handler to allow post-completion status."""
  cls.logger.error(str(e))
  cls.op_failure_count += 1
  cls.logger.debug('\n\nEncountered exception while copying:\n%s\n',
                   traceback.format_exc())


def _RmExceptionHandler(cls, e):
  """Simple exception handler to allow post-completion status."""
  cls.logger.error(str(e))


class CpCommand(Command):
  """Implementation of gsutil cp command.

  Note that CpCommand is run for both gsutil cp and gsutil mv. The latter
  happens by MvCommand calling CpCommand and passing the hidden (undocumented)
  -M option. This allows the copy and remove needed for each mv to run
  together (rather than first running all the cp's and then all the rm's, as
  we originally had implemented), which in turn avoids the following problem
  with removing the wrong objects: starting with a bucket containing only
  the object gs://bucket/obj, say the user does:
    gsutil mv gs://bucket/* gs://bucket/d.txt
  If we ran all the cp's and then all the rm's and we didn't expand the wildcard
  first, the cp command would first copy gs://bucket/obj to gs://bucket/d.txt,
  and the rm command would then remove that object. In the implementation
  prior to gsutil release 3.12 we avoided this by building a list of objects
  to process and then running the copies and then the removes; but building
  the list up front limits scalability (compared with the current approach
  of processing the bucket listing iterator on the fly).
  """

  # Command specification. See base class for documentation.
  command_spec = Command.CreateCommandSpec(
      'cp',
      command_name_aliases=['copy'],
      usage_synopsis=_SYNOPSIS,
      min_args=1,
      max_args=NO_MAX,
      # -t is deprecated but leave intact for now to avoid breakage.
      supported_sub_args=CP_SUB_ARGS,
      file_url_ok=True,
      provider_url_ok=False,
      urls_start_arg=0,
      gs_api_support=[ApiSelector.XML, ApiSelector.JSON],
      gs_default_api=ApiSelector.JSON,
      # Unfortunately, "private" args are the only way to support non-single
      # character flags.
      supported_private_args=['stet', 'testcallbackfile='],
      argparse_arguments=[
          CommandArgument.MakeZeroOrMoreCloudOrFileURLsArgument(),
      ],
  )
  # Help specification. See help_provider.py for documentation.
  help_spec = Command.HelpSpec(
      help_name='cp',
      help_name_aliases=['copy'],
      help_type='command_help',
      help_one_line_summary='Copy files and objects',
      help_text=_DETAILED_HELP_TEXT,
      subcommand_help_text={},
  )

  # TODO(b/206151615) Add mappings for remaining flags.
  gcloud_storage_map = GcloudStorageMap(
      gcloud_command='alpha storage cp',
      flag_map={
          '-r': GcloudStorageFlag('-r'),
          '-R': GcloudStorageFlag('-r'),
          '-e': GcloudStorageFlag('--ignore-symlinks')
      },
  )

  # pylint: disable=too-many-statements
  def CopyFunc(self, copy_object_info, thread_state=None, preserve_posix=False):
    """Worker function for performing the actual copy (and rm, for mv)."""
    gsutil_api = GetCloudApiInstance(self, thread_state=thread_state)

    copy_helper_opts = copy_helper.GetCopyHelperOpts()
    if copy_helper_opts.perform_mv:
      cmd_name = 'mv'
    else:
      cmd_name = self.command_name
    src_url = copy_object_info.source_storage_url
    exp_src_url = copy_object_info.expanded_storage_url
    src_url_names_container = copy_object_info.names_container
    have_multiple_srcs = copy_object_info.is_multi_source_request

    if src_url.IsCloudUrl() and src_url.IsProvider():
      raise CommandException(
          'The %s command does not allow provider-only source URLs (%s)' %
          (cmd_name, src_url))
    if preserve_posix and src_url.IsFileUrl() and src_url.IsStream():
      raise CommandException('Cannot preserve POSIX attributes with a stream.')
    if self.parallel_operations and src_url.IsFileUrl() and src_url.IsStream():
      raise CommandException(
          'Cannot upload from a stream when using gsutil -m option.')
    if have_multiple_srcs:
      copy_helper.InsistDstUrlNamesContainer(
          copy_object_info.exp_dst_url,
          copy_object_info.have_existing_dst_container, cmd_name)

    # Various GUI tools (like the GCS web console) create placeholder objects
    # ending with '/' when the user creates an empty directory. Normally these
    # tools should delete those placeholders once objects have been written
    # "under" the directory, but sometimes the placeholders are left around. We
    # need to filter them out here, otherwise if the user tries to rsync from
    # GCS to a local directory it will result in a directory/file conflict
    # (e.g., trying to download an object called "mydata/" where the local
    # directory "mydata" exists).
    if IsCloudSubdirPlaceholder(exp_src_url):
      # We used to output the message 'Skipping cloud sub-directory placeholder
      # object...' but we no longer do so because it caused customer confusion.
      return

    if copy_helper_opts.use_manifest and self.manifest.WasSuccessful(
        exp_src_url.url_string):
      return

    if copy_helper_opts.perform_mv and copy_object_info.names_container:
      # Use recursion_requested when performing name expansion for the
      # directory mv case so we can determine if any of the source URLs are
      # directories (and then use cp -r and rm -r to perform the move, to
      # match the behavior of Linux mv (which when moving a directory moves
      # all the contained files).
      self.recursion_requested = True

    if (copy_object_info.exp_dst_url.IsFileUrl() and
        not os.path.exists(copy_object_info.exp_dst_url.object_name) and
        have_multiple_srcs):

      try:
        os.makedirs(copy_object_info.exp_dst_url.object_name)
      except OSError as e:
        if e.errno != errno.EEXIST:
          raise

    dst_url = copy_helper.ConstructDstUrl(
        src_url,
        exp_src_url,
        src_url_names_container,
        have_multiple_srcs,
        copy_object_info.is_multi_top_level_source_request,
        copy_object_info.exp_dst_url,
        copy_object_info.have_existing_dst_container,
        self.recursion_requested,
        preserve_posix=preserve_posix)
    dst_url = copy_helper.FixWindowsNaming(src_url, dst_url)

    copy_helper.CheckForDirFileConflict(exp_src_url, dst_url)
    if copy_helper.SrcDstSame(exp_src_url, dst_url):
      raise CommandException('%s: "%s" and "%s" are the same file - '
                             'abort.' % (cmd_name, exp_src_url, dst_url))

    if dst_url.IsCloudUrl() and dst_url.HasGeneration():
      raise CommandException('%s: a version-specific URL\n(%s)\ncannot be '
                             'the destination for gsutil cp - abort.' %
                             (cmd_name, dst_url))

    if not dst_url.IsCloudUrl() and copy_helper_opts.dest_storage_class:
      raise CommandException('Cannot specify storage class for a non-cloud '
                             'destination: %s' % dst_url)

    src_obj_metadata = None
    if copy_object_info.expanded_result:
      src_obj_metadata = encoding.JsonToMessage(
          apitools_messages.Object, copy_object_info.expanded_result)

    if src_url.IsFileUrl() and preserve_posix:
      if not src_obj_metadata:
        src_obj_metadata = apitools_messages.Object()
      mode, _, _, _, uid, gid, _, atime, mtime, _ = os.stat(
          exp_src_url.object_name)
      mode = ConvertModeToBase8(mode)
      posix_attrs = POSIXAttributes(atime=atime,
                                    mtime=mtime,
                                    uid=uid,
                                    gid=gid,
                                    mode=mode)
      custom_metadata = apitools_messages.Object.MetadataValue(
          additionalProperties=[])
      SerializeFileAttributesToObjectMetadata(posix_attrs,
                                              custom_metadata,
                                              preserve_posix=preserve_posix)
      src_obj_metadata.metadata = custom_metadata

    if src_obj_metadata and dst_url.IsFileUrl():
      posix_attrs = DeserializeFileAttributesFromObjectMetadata(
          src_obj_metadata, src_url.url_string)
      mode = posix_attrs.mode.permissions
      valid, err = ValidateFilePermissionAccess(src_url.url_string,
                                                uid=posix_attrs.uid,
                                                gid=posix_attrs.gid,
                                                mode=mode)
      if preserve_posix and not valid:
        logging.getLogger().critical(err)
        raise CommandException('This sync will orphan file(s), please fix their'
                               ' permissions before trying again.')

    bytes_transferred = 0
    try:
      if copy_helper_opts.use_manifest:
        self.manifest.Initialize(exp_src_url.url_string, dst_url.url_string)
      _, bytes_transferred, result_url, md5 = copy_helper.PerformCopy(
          self.logger,
          exp_src_url,
          dst_url,
          gsutil_api,
          self,
          _CopyExceptionHandler,
          src_obj_metadata=src_obj_metadata,
          allow_splitting=True,
          headers=self.headers,
          manifest=self.manifest,
          gzip_encoded=self.gzip_encoded,
          gzip_exts=self.gzip_exts,
          preserve_posix=preserve_posix,
          use_stet=self.use_stet)
      if copy_helper_opts.use_manifest:
        if md5:
          self.manifest.Set(exp_src_url.url_string, 'md5', md5)
        self.manifest.SetResult(exp_src_url.url_string, bytes_transferred, 'OK')
      if copy_helper_opts.print_ver:
        # Some cases don't return a version-specific URL (e.g., if destination
        # is a file).
        self.logger.info('Created: %s', result_url)
    except ItemExistsError:
      message = 'Skipping existing item: %s' % dst_url
      self.logger.info(message)
      if copy_helper_opts.use_manifest:
        self.manifest.SetResult(exp_src_url.url_string, 0, 'skip', message)
    except SkipUnsupportedObjectError as e:
      message = ('Skipping item %s with unsupported object type %s' %
                 (exp_src_url.url_string, e.unsupported_type))
      self.logger.info(message)
      if copy_helper_opts.use_manifest:
        self.manifest.SetResult(exp_src_url.url_string, 0, 'skip', message)
    except copy_helper.FileConcurrencySkipError as e:
      self.logger.warn(
          'Skipping copy of source URL %s because destination URL '
          '%s is already being copied by another gsutil process '
          'or thread (did you specify the same source URL twice?) ' %
          (src_url, dst_url))
    except Exception as e:  # pylint: disable=broad-except
      if (copy_helper_opts.no_clobber and
          copy_helper.IsNoClobberServerException(e)):
        message = 'Rejected (noclobber): %s' % dst_url
        self.logger.info(message)
        if copy_helper_opts.use_manifest:
          self.manifest.SetResult(exp_src_url.url_string, 0, 'skip', message)
      elif self.continue_on_error:
        message = 'Error copying %s: %s' % (src_url, str(e))
        self.op_failure_count += 1
        self.logger.error(message)
        if copy_helper_opts.use_manifest:
          self.manifest.SetResult(exp_src_url.url_string, 0, 'error',
                                  RemoveCRLFFromString(message))
      else:
        if copy_helper_opts.use_manifest:
          self.manifest.SetResult(exp_src_url.url_string, 0, 'error', str(e))
        raise
    else:
      if copy_helper_opts.perform_mv:
        self.logger.info('Removing %s...', exp_src_url)
        if exp_src_url.IsCloudUrl():
          gsutil_api.DeleteObject(exp_src_url.bucket_name,
                                  exp_src_url.object_name,
                                  generation=exp_src_url.generation,
                                  provider=exp_src_url.scheme)
        else:
          os.unlink(exp_src_url.object_name)

    with self.stats_lock:
      # TODO: Remove stats_lock; we should be able to calculate bytes
      # transferred from StatusMessages posted by operations within PerformCopy.
      self.total_bytes_transferred += bytes_transferred

  def _ConstructNameExpansionIteratorDstTupleIterator(self, src_url_strs_iter,
                                                      dst_url_strs):
    copy_helper_opts = copy_helper.GetCopyHelperOpts()
    for src_url_str, dst_url_str in zip(src_url_strs_iter, dst_url_strs):
      # Getting the destination information for each (sources, destination)
      # tuple. This assumes that the same destination is never provided in
      # multiple tuples, and doing so may result in an inconsistent behavior
      # especially when using the -m multi-threading option.
      #
      # Example for the inconsistent behavior, the following commands will
      # behave differently:
      #
      # gsutil cp -r dir1 dir2 gs://bucket/non-existent-dir
      # gsutil cp -r [
      #            (dir1, gs://bucket/non-existent-dir),
      #            (dir2, gs://bucket/non-existent-dir)
      #        ]
      #
      # When multiple threads execute on a non existing destination directory.
      # These threads might encounter different states of the destination
      # directory. The first thread to execute the command finds that the
      # destination directory does not exist, it will create the destination
      # directory and copies the files inside the source directories to the
      # destination directory. The following threads find that the destination
      # directory already exists and copy the source directories in the
      # destination directory. In another scenario, all the threads might find
      # that the destination directory does not exist and copy the source
      # directories to the destination directory.
      exp_dst_url, have_existing_dst_container = (
          copy_helper.ExpandUrlToSingleBlr(dst_url_str,
                                           self.gsutil_api,
                                           self.project_id,
                                           logger=self.logger))
      name_expansion_iterator_dst_tuple = NameExpansionIteratorDestinationTuple(
          NameExpansionIterator(
              self.command_name,
              self.debug,
              self.logger,
              self.gsutil_api,
              src_url_str,
              self.recursion_requested or copy_helper_opts.perform_mv,
              project_id=self.project_id,
              all_versions=self.all_versions,
              ignore_symlinks=self.exclude_symlinks,
              continue_on_error=(self.continue_on_error or
                                 self.parallel_operations),
              bucket_listing_fields=GetSourceFieldsNeededForCopy(
                  exp_dst_url.IsCloudUrl(),
                  copy_helper_opts.skip_unsupported_objects,
                  copy_helper_opts.preserve_acl,
                  preserve_posix=self.preserve_posix_attrs,
                  delete_source=copy_helper_opts.perform_mv,
                  file_size_will_change=self.use_stet)),
          DestinationInfo(exp_dst_url, have_existing_dst_container))

      self.has_file_dst = self.has_file_dst or exp_dst_url.IsFileUrl()
      self.has_cloud_dst = self.has_cloud_dst or exp_dst_url.IsCloudUrl()
      self.provider_types.add(exp_dst_url.scheme)
      self.combined_src_urls = itertools.chain(self.combined_src_urls,
                                               src_url_str)

      yield name_expansion_iterator_dst_tuple

  # Command entry point.
  def RunCommand(self):
    copy_helper_opts = self._ParseOpts()

    self.total_bytes_transferred = 0

    dst_url = StorageUrlFromString(self.args[-1])
    if dst_url.IsFileUrl() and (dst_url.object_name == '-' or dst_url.IsFifo()):
      if self.preserve_posix_attrs:
        raise CommandException('Cannot preserve POSIX attributes with a '
                               'stream or a named pipe.')
      cat_out_fd = (GetStreamFromFileUrl(dst_url, mode='wb')
                    if dst_url.IsFifo() else None)
      return cat_helper.CatHelper(self).CatUrlStrings(self.args[:-1],
                                                      cat_out_fd=cat_out_fd)

    if copy_helper_opts.read_args_from_stdin:
      if len(self.args) != 1:
        raise CommandException('Source URLs cannot be specified with -I option')
      # Use StdinIteratorCls instead of StdinIterator here to avoid Python 3
      # generator pickling errors when multiprocessing a command.
      src_url_strs = [StdinIteratorCls()]
    else:
      if len(self.args) < 2:
        raise CommandException('Wrong number of arguments for "cp" command.')
      src_url_strs = [self.args[:-1]]

    dst_url_strs = [self.args[-1]]

    self.combined_src_urls = []
    self.has_file_dst = False
    self.has_cloud_dst = False
    self.provider_types = set()
    # Because cp may have multiple source URLs and multiple destinations, we
    # wrap the name expansion iterator in order to collect analytics.
    name_expansion_iterator = CopyObjectsIterator(
        self._ConstructNameExpansionIteratorDstTupleIterator(
            src_url_strs, dst_url_strs),
        copy_helper_opts.daisy_chain,
    )

    seek_ahead_iterator = None
    # Cannot seek ahead with stdin args, since we can only iterate them
    # once without buffering in memory.
    if not copy_helper_opts.read_args_from_stdin:
      seek_ahead_iterator = SeekAheadNameExpansionIterator(
          self.command_name,
          self.debug,
          self.GetSeekAheadGsutilApi(),
          self.combined_src_urls,
          self.recursion_requested or copy_helper_opts.perform_mv,
          all_versions=self.all_versions,
          project_id=self.project_id,
          ignore_symlinks=self.exclude_symlinks,
          file_size_will_change=self.use_stet)

    # Use a lock to ensure accurate statistics in the face of
    # multi-threading/multi-processing.
    self.stats_lock = parallelism_framework_util.CreateLock()

    # Tracks if any copies failed.
    self.op_failure_count = 0

    # Start the clock.
    start_time = time.time()

    # Tuple of attributes to share/manage across multiple processes in
    # parallel (-m) mode.
    shared_attrs = ('op_failure_count', 'total_bytes_transferred')

    # Perform copy requests in parallel (-m) mode, if requested, using
    # configured number of parallel processes and threads. Otherwise,
    # perform requests with sequential function calls in current process.
    self.Apply(_CopyFuncWrapper,
               name_expansion_iterator,
               _CopyExceptionHandler,
               shared_attrs,
               fail_on_error=(not self.continue_on_error),
               seek_ahead_iterator=seek_ahead_iterator)
    self.logger.debug('total_bytes_transferred: %d',
                      self.total_bytes_transferred)

    end_time = time.time()
    self.total_elapsed_time = end_time - start_time
    self.total_bytes_per_second = CalculateThroughput(
        self.total_bytes_transferred, self.total_elapsed_time)
    LogPerformanceSummaryParams(
        has_file_dst=self.has_file_dst,
        has_cloud_dst=self.has_cloud_dst,
        avg_throughput=self.total_bytes_per_second,
        total_bytes_transferred=self.total_bytes_transferred,
        total_elapsed_time=self.total_elapsed_time,
        uses_fan=self.parallel_operations,
        is_daisy_chain=copy_helper_opts.daisy_chain,
        provider_types=list(self.provider_types))

    if self.debug >= DEBUGLEVEL_DUMP_REQUESTS:
      # Note that this only counts the actual GET and PUT bytes for the copy
      # - not any transfers for doing wildcard expansion, the initial
      # HEAD/GET request performed to get the object metadata, etc.
      if self.total_bytes_transferred != 0:
        self.logger.info(
            'Total bytes copied=%d, total elapsed time=%5.3f secs (%sps)',
            self.total_bytes_transferred, self.total_elapsed_time,
            MakeHumanReadable(self.total_bytes_per_second))
    if self.op_failure_count:
      plural_str = 's' if self.op_failure_count > 1 else ''
      raise CommandException('{count} file{pl}/object{pl} could '
                             'not be transferred.'.format(
                                 count=self.op_failure_count, pl=plural_str))

    return 0

  def _ParseOpts(self):
    # TODO: Arrange variables initialized here in alphabetical order.
    perform_mv = False
    # exclude_symlinks is handled by Command parent class, so save in Command
    # state rather than CopyHelperOpts.
    self.exclude_symlinks = False
    no_clobber = False
    # continue_on_error is handled by Command parent class, so save in Command
    # state rather than CopyHelperOpts.
    self.continue_on_error = False
    daisy_chain = False
    read_args_from_stdin = False
    print_ver = False
    use_manifest = False
    preserve_acl = False
    self.preserve_posix_attrs = False
    canned_acl = None
    # canned_acl is handled by a helper function in parent
    # Command class, so save in Command state rather than CopyHelperOpts.
    self.canned = None

    self.all_versions = False

    self.skip_unsupported_objects = False

    # Files matching these extensions should be compressed.
    # The gzip_encoded flag marks if the files should be compressed during
    # the upload. The gzip_local flag marks if the files should be compressed
    # before uploading. Files compressed prior to uploaded are stored
    # compressed, while files compressed during the upload are stored
    # uncompressed. These flags cannot be mixed.
    gzip_encoded = False
    gzip_local = False
    gzip_arg_exts = None
    gzip_arg_all = None

    test_callback_file = None
    dest_storage_class = None
    self.use_stet = False

    # self.recursion_requested initialized in command.py (so can be checked
    # in parent class for all commands).
    self.manifest = None
    if self.sub_opts:
      for o, a in self.sub_opts:
        if o == '-a':
          canned_acl = a
          self.canned = True
        if o == '-A':
          self.all_versions = True
        if o == '-c':
          self.continue_on_error = True
        elif o == '-D':
          daisy_chain = True
        elif o == '-e':
          self.exclude_symlinks = True
        elif o == '--testcallbackfile':
          # File path of a pickled class that implements ProgressCallback.call.
          # Used for testing transfer interruptions and resumes.
          test_callback_file = a
        elif o == '-I':
          read_args_from_stdin = True
        elif o == '-j':
          gzip_encoded = True
          gzip_arg_exts = [x.strip() for x in a.split(',')]
        elif o == '-J':
          gzip_encoded = True
          gzip_arg_all = GZIP_ALL_FILES
        elif o == '-L':
          use_manifest = True
          self.manifest = Manifest(a)
        elif o == '-M':
          # Note that we signal to the cp command to perform a move (copy
          # followed by remove) and use directory-move naming rules by passing
          # the undocumented (for internal use) -M option when running the cp
          # command from mv.py.
          perform_mv = True
        elif o == '-n':
          no_clobber = True
        elif o == '-p':
          preserve_acl = True
        elif o == '-P':
          self.preserve_posix_attrs = True
          InitializeUserGroups()
        elif o == '-r' or o == '-R':
          self.recursion_requested = True
        elif o == '-s':
          dest_storage_class = NormalizeStorageClass(a)
        elif o == '-U':
          self.skip_unsupported_objects = True
        elif o == '-v':
          print_ver = True
        elif o == '-z':
          gzip_local = True
          gzip_arg_exts = [x.strip() for x in a.split(',')]
        elif o == '-Z':
          gzip_local = True
          gzip_arg_all = GZIP_ALL_FILES
        elif o == '--stet':
          self.use_stet = True

    if preserve_acl and canned_acl:
      raise CommandException(
          'Specifying both the -p and -a options together is invalid.')

    if self.all_versions and self.parallel_operations:
      raise CommandException(
          'The gsutil -m option is not supported with the cp -A flag, to '
          'ensure that object version ordering is preserved. Please re-run '
          'the command without the -m option.')
    if gzip_encoded and gzip_local:
      raise CommandException(
          'Specifying both the -j/-J and -z/-Z options together is invalid.')
    if gzip_arg_exts and gzip_arg_all:
      if gzip_encoded:
        raise CommandException(
            'Specifying both the -j and -J options together is invalid.')
      else:
        raise CommandException(
            'Specifying both the -z and -Z options together is invalid.')
    self.gzip_exts = gzip_arg_exts or gzip_arg_all
    self.gzip_encoded = gzip_encoded

    return CreateCopyHelperOpts(
        perform_mv=perform_mv,
        no_clobber=no_clobber,
        daisy_chain=daisy_chain,
        read_args_from_stdin=read_args_from_stdin,
        print_ver=print_ver,
        use_manifest=use_manifest,
        preserve_acl=preserve_acl,
        canned_acl=canned_acl,
        skip_unsupported_objects=self.skip_unsupported_objects,
        test_callback_file=test_callback_file,
        dest_storage_class=dest_storage_class)
