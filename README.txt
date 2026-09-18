lllzorb 0.1

Live Local Linux ZFS OS Recovery & Backup

WARNING: RISK OF PERMANENT DATA LOSS — USE ENTIRELY AT YOUR OWN RISK
-----------------------------------------------------------------
THIS SOFTWARE CAN PERMANENTLY ERASE DISKS, DESTROY BACKUPS, CORRUPT DATA, AND LEAVE SYSTEMS UNBOOTABLE.
BUGS, INTERRUPTED OPERATIONS, INCORRECT DEVICE SELECTION, OR UNEXPECTED SYSTEM BEHAVIOR CAN CAUSE
IRREVERSIBLE LOSS. A SUCCESS MESSAGE DOES NOT GUARANTEE THAT A BACKUP IS COMPLETE OR RESTORABLE.
DO NOT USE THIS SOFTWARE AS YOUR ONLY BACKUP. KEEP INDEPENDENT, VERIFIED BACKUPS AND TEST RESTORES
ON DISPOSABLE HARDWARE BEFORE RELYING ON IT. YOU ARE RESPONSIBLE FOR VERIFYING EVERY SELECTED DISK.

THIS SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND. TO THE MAXIMUM EXTENT PERMITTED
BY APPLICABLE LAW, THE AUTHOR AND CONTRIBUTORS ACCEPT NO RESPONSIBILITY OR LIABILITY FOR ANY DATA
LOSS, CORRUPTION, HARDWARE DAMAGE, DOWNTIME, LOST PROFITS, RECOVERY COSTS, OR OTHER DAMAGE ARISING
FROM USING OR BEING UNABLE TO USE THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
YOU ASSUME THE ENTIRE RISK AND COST OF USING IT AND OF ANY NECESSARY RECOVERY, REPAIR, OR RESTORATION.
THE WARRANTY DISCLAIMERS AND LIABILITY LIMITATIONS IN SECTIONS 6 AND 7 OF THE LICENSE ALSO APPLY.

License
-------
This project is licensed under the Mozilla Public License 2.0 (SPDX: MPL-2.0).
See LICENSE for the complete, unmodified license text, or https://mozilla.org/MPL/2.0/.

What is it?
------------

A tool that lets you do a live backup of your Linux ZFS root boot OS disk.

Why live?
----------

My Proxmox machine runs critical services, in particular my router VM and Home Assistant OS VMs.
Temporarily rebooting to do a Clonezilla or other offline backup is not acceptable.
These services must always remain up during backups. I can't have my entire network go down.
As HAOS manages my lighting system also, that means being unable to turn lights on/off during downtime.
Such downtime needs to be limited to maintenance windows or unavoidable disaster recovery cases, not
routine backups.

Why local?
-----------
In case of disaster, the router will be down, and thus the entire LAN/WLAN will be, too, as well as
lights and switches.
Reconstructing the router boot disk is the purpose of the restore, and the restore process
thus can't depend on a remote server.
A lengthy restore process is also not ideal, and should only need to be performed in case of
hardware failure. The most common intended usage case is one where I make live backups of the boot
disk, and periodically restore them to spare boot disks that are ready to be reinserted into the
host, should its boot disk fail. The downtime is thus brought down to seconds - removing the
failed SSD boot disk from the front SATA hotswap bay, and inserting a clone.
If a clone is unavailable, a lengthier local restore process is possible, but there is currently
no boot media to do so.

Why Linux?
-----------
Because that's what my Proxmox server boots from.

Why ZFS?
---------
The OS must boot from ZFS root. This is because ZFS allows consistent snapshots of the root file systems.
Prior to switching to ZFS, I used ext4 root. The only available live backup method I could find was dd.
While it worked, dd did not provide a consistent backup, as the drive content could change in the middle of the backup.
A dd backup is also impossible to restore unmodified onto a smaller drive than the original backup drive.

ZFS must also be used on the backup target. This is to take advantage of ZFS snapshots, and do incremental backups.

Tested OS
---------
Proxmox 9.2, booting from ZFS root from a SATA SSD inserted into a hotswap front bay, connected to the Asus Prime X570 Pro motherboard SATA controller.
Ubuntu 26.04, booting from ZFS root from a SATA SSD inserted into a hotswap front bay, connected to the Asus Z170-AR motherboard SATA controller.

Tested backup targets
---------------------
SATA SSD attached to StarTech USB 3.0 to SATA bridge
Samsung T9 USB SSD

Usage
-----

1. backup command
-----------------

a. target disk selection
lllzorb attempts to automatically detect an existing backup drive. If there is one, and only one, it will be automatically selected.
In all other cases, it will present a list of target disk candidates. The list includes any detected backup drive, and any other drive
that's currently not mounted. If no drive is eligible for backup, you will be prompted to attach one to the system.
The first time a backup is performed, you will need to select a drive. It will be destroyed and repartitioned with zfs.
The backup command will prompt for confirmation for this operation.

Note that the backups will be indexed by hostname, so that a single target can be used to backup/restore multiple hosts.

Ctrl-C requests cancellation and waits for transfer cleanup before export. Further Ctrl-C signals are ignored.
If a transfer process cannot exit, the pool stays imported and locked until that process exits.

b. source disk selection
Selection is automatic, based on the detected boot disk.

c. source snapshot selection.
By default, backup will make new snapshots of the datasets on the boot disk, and select that snapshot for backup.
The first backup will be a full backup. For subsequent backups, the existing chain of backups will be compared with the
existing snapshots on the source disk. If there is a common snapshot, the backup will be incremental rather than full.

d. non-interactive backup mode
If there is only a single existing backup disk connected, and no unmounted target disks, the backup will run without an interactive prompt.

e. command-line flags

--compression <method>
        Set compression on all backup target datasets, including boot pool copies. Examples: lz4, zstd, zstd-3, gzip-6, off.
        Required compression features must already be enabled on the backup storage pool.
        Original source compression settings are retained for restore, including those for Ubuntu bpool.
        Raw encrypted streams cannot be recompressed. Incremental backups apply the method to newly written blocks;
        existing blocks are not rewritten.

--diagnostic
        Logs transfer stages, process IDs and wait states, pool I/O, command timings and cleanup.
        Writes to the terminal and lllzorb-diagnostic-<timestamp>-<pid>.log in the current directory.

--ephemeral
        Creates a full backup and removes its temporary source snapshots after completion, including normal failure or cancellation.
        Existing snapshots and snapshots created by other operations are preserved. No clone is created.
        These backups can ONLY be restored with a full restore, NOT an incremental restore, and are not used as incremental backup bases.
        Cannot be combined with --incremental, --stack, --snapshot_name, or --snapshot_index. --full is optional.
        If transfer processes cannot stop or cleanup fails, temporary snapshots may remain; the program reports this.
        A crash or forced termination can also leave temporary snapshots behind.

--full
        forces a full backup

--incremental
        forces an incremental backup. If no matching base is found, abort.

--stack
        backs up all intermediate snapshots from the source disk, rather than just the latest one

--unattended
        Forces non-interactive mode. If there is any ambiguity in the backup target, such as an unmounted disk that is not a previous backup target,
        that would otherwise result in a prompt, returns a non-zero error code to the caller. No disks are ever erased in this mode.

--destination <disk_id>
        ID of backup disk target.

--snapshot_name <snapshot>
        Name of snapshot to backup. Can't be combined with snapshot_index.

--snapshot_index <number>
        1 is the most recent snapshot. Can't be combined with snapshot_name.

--host
        Which hostname to backup under. Defaults to the local system hostname.


2. restore command
------------------

a. source disk selection
Enumeration is automatic. If there is more than one, you will be prompted to select it. If there is just one, it
will be automatically selected.

b. source host selection
If multiple hosts have been backed up onto the same backup disk, you will be prompted to select the host to restore.

c. snapshot selection
You will be presented with a list of snapshots for the given host. The most recent snapshot is selected by default.

d. target disk selection.
You will be presented with a list of unmounted drives eligible for restore.

e. swap partition size selection
If the snapshot contains a swap partition, this allows resizing it during a full restore. This can be useful if the OS was installed
with a swap partition smaller than the total RAM in the original host, which can prevent hibernation. I ran into this on my Ubuntu
26 with 24GB, when installing as ZFS root onto a 128GB SSD. The resulting swap partition was only 8GB.

f. final confirmation
You will be prompted to confirm the drive to overwrite during restore.
Ctrl-C requests cancellation and waits for cleanup. Further Ctrl-C signals are ignored during cleanup.
An interrupted restore can leave the destination incomplete.

All full and incremental restores temporarily use sync=disabled on the destination for performance, whether run on the original
host or another host. Before successful completion, each dataset's saved sync setting is restored and zpool sync flushes pending
writes before export. sync=disabled remains in effect afterward only for datasets whose saved setting was disabled.
These temporary settings apply only to the destination; the running OS pools are unchanged.

g. command-line flags

--stack
        Full restore includes all saved history through the selected snapshot; without this flag, only the selected snapshot is sent.
        Incremental restore includes intermediate snapshots between the common base and selected snapshot.
        Without this flag, only the common base and selected snapshot remain on the target; other snapshots are removed after confirmation.
        If the selected snapshot is already the base, only that snapshot remains. Holds or clone dependencies can prevent removal.
        Full selected-only restore of inherited encryption or encrypted clones requires --stack.

--compression <method>
        By default, the original dataset compression method will be used during restore. This setting allows customizing
        compression, and applies to all datasets, except a separate boot pool, such as the Ubuntu boot pool.
        The Proxmox rpool is eligible for any compression setting.

--swap <MB>
        Specify the target swap partition size in MB. Not allowed with --incremental.
        Unattended full restore keeps the original backup swap size when omitted.

--incremental
        Performs an incremental restore, rather than fully destroying the target, to speed up the restore, and save
        wear and tear on flash or HDD. This requires the partition layout to match the backup, other than partition sizes.
        There also needs to be a common snapshot between the selected backup and target. If there is no match, the restore
        will abort. Unsnapshotted changes in the destination, and intermediate snapshots since the common base, will be overwritten.

--unattended
        Forces non-interactive mode. Requires --source, --destination and --confirm. Defaults to restoring the latest snapshot.
        Supports full and incremental restore. Full restore destroys the destination disk; incremental restore replaces target data.
        Ambiguity returns a non-zero error instead of prompting. Specify --host when the backup contains multiple hosts.
        Encrypted restores that may require key entry are rejected before modifying the destination.

--confirm
        Required with --unattended: authorizes full-target destruction or incremental replacement without a confirmation prompt.
        Cannot be used without --unattended.

--discard <on|off>
        Defaults to off. With on, discard the confirmed target before full restore. Any discard failure aborts restore.
        --discard on is not allowed with --incremental.

--allow-small-target
        Allow full restore despite an estimated capacity shortfall. Out-of-space failure remains possible.
        Without this flag, unattended restore rejects the shortfall. In interactive mode, you will be prompted.

--dry-run
        Validate and show the proposed restore without writing the target.

--list-snapshots
        List available recovery points and exit without selecting a restore destination.

--source <disk_id>
        ID of restore source disk.

--destination <disk_id>
        ID of restore disk target.

--host
        Hostname to restore.

--snapshot_name <snapshot>
        Name of snapshot to restore. Mutually exclusive with --snapshot_index.

--snapshot_index <number>
        1 is the most recent snapshot. Mutually exclusive with --snapshot_name.

--diagnostic
        Logs transfer stages, process IDs and wait states, pool allocation and I/O, and command timings.
        Includes receive failures and pool export cleanup. Writes to the terminal and to
        lllzorb-diagnostic-<timestamp>-<pid>.log in the current directory.

3. snapshots command
--------------------

Snapshot listings and backup/restore output show compressed and uncompressed dataset sizes.
These use referenced and logicalreferenced bytes, include metadata, and exclude other snapshot history.
Shared blocks may be counted more than once. Backup/restore sizes are recorded on the source at the selected snapshot;
destination allocation can differ. Dataset sizes are separate from transfer estimates and space reclaimed by deletion.
This is used to list and purge backup snapshots on either the boot OS disk or the backup target disk. The command is interactive.
If intermediate snapshots have been taken externally, in the middle of the backup chain, there should be extra warnings when purging.

Backup and restore report actual destination compressed and uncompressed sizes alongside the ZFS compression ratio after syncing.
These cover the destination dataset tree, including retained snapshots and existing data from earlier incremental operations.
Compressed size counts allocated dataset and snapshot space, excluding unused reservations; uncompressed size uses logicalused.
Sizes include metadata. Per-pool ratios come directly from ZFS. The Overall row sums the sizes across all pools and
divides total uncompressed bytes by total compressed bytes; it does not average the per-pool ratios.
Backup shows one pool summary and an updating progress/completion line. Per-dataset pipeline messages require --diagnostic.
Final backup pool, overall and transfer-total lines show the selected compression method; without --compression they show no override.
Default status, transfer progress and completion output includes wall-clock and elapsed timestamps.
A final line gives total elapsed time, actual ZFS stream MB transferred and average MB/s (1 MB = 1,000,000 bytes), after cleanup.
The average divides bytes actually sent to receivers during this run by the total duration, including preparation, transfer,
verification and cleanup. The restore timer starts after confirmation; the backup timer starts with the command.
Incremental totals exclude retained data; estimates and destination dataset sizes are never used for transfer rates.
Stream bytes include ZFS stream headers. Destination compression occurs separately, so this is not a physical disk-write rate.

Concurrent operations
---------------------
Local locks coordinate operations without a daemon. Each restore destination is locked from selection through cleanup,
including cancellation. Restores to different disks may run concurrently; two restores to the same disk are rejected,
even when different hardware-ID aliases are used. Only one backup may write to a given backup disk at a time.
Backups and restores can share a backup disk. A backup or purge of a host currently being restored is rejected.
The backup pool stays imported until its last user finishes. Pools already imported before the operation remain imported.
Lock conflicts return a non-zero exit code. Locks are also retained by transfer children until they exit.

Same-host restore
-----------------
Same-host restore means restoring a backup onto a separate idle disk attached to the currently running host.
It does not restore onto or overwrite the host's currently running boot disk. When the original pools are imported, the destination
keeps new pool identities and its boot configuration and initramfs images are updated. The running OS pools are never renamed,
exported or assigned new identities. Existing backups work; no new backup or extra flag is required.
Incremental restore also supports these targets, provided the partition layout matches and a common snapshot remains.
Dracut initramfs images are rebuilt and validated during both full and incremental restores.
Restored fstab entries use destination disk paths; UUID and PARTUUID references are updated if the destination identifiers differ.
An older target sharing an imported pool's identity needs a full restore first, or a recovery environment without that pool imported.
Use the completed disk in place of the original when booting; partition and filesystem identifiers are still preserved.

Requirements
------------
Live backup operation is non-destructive to the currently running OS. No existing datasets are ever unmounted. The only thing that gets written
to the boot disk during backup is the new snapshots that are taken.
Tested on OpenZFS 2.4.4.

Current limitations
-------------------
Only single-disk OS ZFS boot configurations are supported, not RAID.

Only single-disk target disk configurations are supported, not RAID.

Virtual disks which don't have unique IDs are not supported.

Multiple snapshot branches, from clones, are not supported.

Backup/restore onto a LAN ZFS target, as opposed to a locally attached drive, has not been tested.

There is no GUI.
