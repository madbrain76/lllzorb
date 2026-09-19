# SPDX-License-Identifier: MPL-2.0
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

import copy
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zlib
import sys
import importlib.machinery
import importlib.util
loader=importlib.machinery.SourceFileLoader('lllzorb',str(Path(__file__).resolve().parents[1]/'lllzorb'))
spec=importlib.util.spec_from_loader(loader.name,loader)
z=importlib.util.module_from_spec(spec)
loader.exec_module(z)


def setUpModule():
    global lock_root, lock_patch
    lock_root=tempfile.TemporaryDirectory(prefix='zfs-test-locks-')
    lock_patch=patch.object(z,'LOCK_DIRECTORY',Path(lock_root.name))
    lock_patch.start()


def tearDownModule():
    lock_patch.stop()
    lock_root.cleanup()


def fixture():
    parts=[]
    start=z.MIB//512
    # Root pool in the middle, partition numbering deliberately differs from order.
    for number,kind,size,pool in [(4,'esp',512*z.MIB,None),(2,'zfs',1800*z.GIB,'tank'),(7,'esp',2*z.GIB,None)]:
        p=dict(number=number,kind=kind,start_lba=start,end_lba=start+size//512-1,
               size_bytes=size,type_guid=z.ESP if kind=='esp' else z.ZFS,partuuid=str(uuid.uuid4()),
               name=pool or 'EFI',attributes=0,source_device=f'/dev/fake{number}')
        if pool:p['pool']=pool
        else:p.update(fat_bits=32,fat_uuid='ABCD-1234',fat_label='EFI',archive=f'efi/esp-{number}.tar.zst',mountpoints=['/boot/efi'] if number==4 else [])
        parts.append(p);start+=size//512
    pools=[]
    for name,num,used in [('tank',2,287*z.GIB)]:
        ds={name:{'encryption':{'value':'off'},'used':{'value':str(used)}}}
        if name=='tank':ds['tank/ROOT/ubuntu']={}
        pools.append(dict(name=name,guid='123',topology='stripe',ashift=12,partitions=[num],
                          datasets=ds,properties={},bootfs='tank/ROOT/ubuntu' if name=='tank' else '-',
                          allocated_bytes=used,used_bytes=used,logicalused_bytes=used,referenced_bytes=used,
                          estimated_send_bytes=used,stream_bytes=used,stream=f'zfs/{name}.zfs',encrypted=False))
    return dict(version=1,backup_type='full',snapshot='baremetal-20260914-183000',
                disk=dict(table_type='gpt',guid=str(uuid.uuid4()),entry_count=128,size_bytes=2000*z.GIB,
                          logical_sector_size=512,physical_sector_size=4096,partitions=parts),
                pools=pools,root_dataset='tank/ROOT/ubuntu',
                mounts=[dict(source='tank/ROOT/ubuntu',target='/',fstype='zfs')],
                boot=dict(architecture='x86_64',signed_efi=True,fstab='UUID=ABCD-1234 /boot/efi vfat defaults 0 1\n'))


class LayoutTests(unittest.TestCase):
    def test_smaller_with_middle_root(self):
        m=fixture();p=z.solve(m,500*z.GIB,512)['partitions']
        self.assertEqual([x['number'] for x in p],[4,2,7])
        self.assertEqual(p[0]['size_bytes'],512*z.MIB)
        self.assertLess(p[1]['size_bytes'],m['disk']['partitions'][1]['size_bytes'])
        self.assertLess(p[-1]['end_lba']*512,500*z.GIB)
        self.assertTrue(all(a['end_lba']<b['start_lba'] for a,b in zip(p,p[1:])))

    def test_too_small(self):
        with self.assertRaises(z.Error):z.solve(fixture(),100*z.GIB,512)

    def test_sector_mismatch(self):
        with self.assertRaises(z.Error):z.solve(fixture(),2000*z.GIB,4096)

    def test_growth_shifts_partition_after_root(self):
        m=fixture()
        a=z.solve(m,2000*z.GIB,512)['partitions']
        b=z.solve(m,3000*z.GIB,512)['partitions']
        self.assertEqual(a[0],b[0]);self.assertEqual(a[1]['start_lba'],b[1]['start_lba'])
        self.assertEqual(b[1]['size_bytes']-a[1]['size_bytes'],1000*z.GIB)
        self.assertEqual(b[2]['start_lba']-a[2]['start_lba'],1000*z.GIB//512)

    def test_four_k(self):
        m=fixture();m['disk']['logical_sector_size']=4096
        for p in m['disk']['partitions']:
            p['start_lba']//=8;p['end_lba']=(p['end_lba']+1)//8-1
        p=z.solve(m,500*z.GIB,4096)['partitions']
        self.assertTrue(all(x['start_lba']%(z.MIB//4096)==0 for x in p))

    def test_multiple_partitions_in_one_pool_rejected(self):
        m=fixture();p=copy.deepcopy(m['disk']['partitions'][1]);p.update(number=8,start_lba=m['disk']['partitions'][-1]['end_lba']+1)
        p['end_lba']=p['start_lba']+p['size_bytes']//512-1
        m['disk']['partitions'].append(p);m['pools'][0]['partitions'].append(8)
        with self.assertRaises(z.Error):z.solve(m,500*z.GIB,512)
        with self.assertRaises(z.Error):z.validate(m)

    def test_non_aligned_fixed_size_preserved(self):
        m=fixture();p=m['disk']['partitions'][0];p['end_lba']-=1;p['size_bytes']-=512
        self.assertEqual(z.solve(m,500*z.GIB,512)['partitions'][0]['size_bytes'],p['size_bytes'])

    def test_removed_layout_flags_are_not_in_help(self):
        root=Path(__file__).resolve().parents[1]
        for command in ('backup','restore','snapshots'):
            result=z.subprocess.run([sys.executable,str(root/'lllzorb'),command,'--help'],capture_output=True,text=True)
            self.assertEqual(result.returncode,0)
            self.assertNotIn('--margin',result.stdout)
            self.assertNotIn('--expand-partition',result.stdout)

    def test_single_zfs_partition_fills_usable_space(self):
        m=fixture();plan=z.solve(m,500*z.GIB,512)
        parts=plan['partitions']
        self.assertEqual(parts[-1]['end_lba']+1,(500*z.GIB-512-m['disk']['entry_count']*128)//z.MIB*z.MIB//512)
        self.assertTrue(all(a['end_lba']+1==b['start_lba'] for a,b in zip(parts,parts[1:])))

    def test_no_free_space_reserve(self):
        m=fixture();plan=z.solve(m,300*z.GIB,512)
        self.assertEqual(plan['pools']['tank']['minimum_partition_bytes'],287*z.GIB+64*z.MIB)

    def test_reservation_included(self):
        m=fixture();m['pools'][0]['datasets']['tank']['reservation']={'value':str(100*z.GIB)}
        self.assertGreater(z.solve(m,800*z.GIB,512)['minimum_bytes'],z.solve(fixture(),800*z.GIB,512)['minimum_bytes'])

    def test_many_sizes_nonoverlapping(self):
        for size in range(380,3100,17):
            p=z.solve(fixture(),size*z.GIB,512)['partitions']
            self.assertTrue(all(a['end_lba']<b['start_lba'] for a,b in zip(p,p[1:])))
            self.assertLess(p[-1]['end_lba']*512,size*z.GIB)


class RestoreCapacityOverrideTests(unittest.TestCase):
    def test_estimated_overflow_can_produce_valid_smaller_layout(self):
        m=fixture();plan=z.solve(m,100*z.GIB,512,allow_estimated_overflow=True)
        self.assertTrue(plan['capacity_warnings'])
        self.assertGreater(plan['minimum_bytes'],100*z.GIB)
        parts=plan['partitions']
        self.assertTrue(all(p['size_bytes']>0 for p in parts))
        self.assertTrue(all(a['end_lba']<b['start_lba'] for a,b in zip(parts,parts[1:])))
        self.assertLess(parts[-1]['end_lba']*512,100*z.GIB)
        self.assertEqual(parts[0]['size_bytes'],m['disk']['partitions'][0]['size_bytes'])

    def test_override_never_allows_impossible_fixed_layout(self):
        with self.assertRaisesRegex(z.Error,'fixed boot/swap partitions'):
            z.solve(fixture(),2*z.GIB,512,allow_estimated_overflow=True)
        with self.assertRaisesRegex(z.Error,'Different logical sector'):
            z.solve(fixture(),100*z.GIB,4096,allow_estimated_overflow=True)

    def test_preserved_boot_pool_estimate_can_be_overridden(self):
        m=ubuntu_fixture();boot=m['pools'][0];boot['estimated_send_bytes']=4*z.GIB
        plan=z.solve(m,64*z.GIB,512,allow_estimated_overflow=True)
        self.assertIn('preserved boot-pool',plan['capacity_warnings'][0])
        self.assertEqual(plan['partitions'][1]['size_bytes'],m['disk']['partitions'][1]['size_bytes'])

    def test_interactive_acceptance_and_default_decline(self):
        plan={'capacity_warnings':['Estimated data exceeds capacity']}
        for answer in ('y','yes','','n'):
            with self.subTest(answer=answer),patch('builtins.input',return_value=answer) as prompt, \
                 patch('sys.stdout',new_callable=io.StringIO):
                if answer in ('y','yes'):z.confirm_restore_capacity(plan,z.argparse.Namespace(dry_run=False))
                else:
                    with self.assertRaisesRegex(z.Error,'not accepted'):
                        z.confirm_restore_capacity(plan,z.argparse.Namespace(dry_run=False))
                prompt.assert_called_once()

    def test_unattended_requires_explicit_capacity_override(self):
        plan={'capacity_warnings':['Estimated data exceeds capacity']}
        with patch('builtins.input') as prompt,patch('sys.stdout',new_callable=io.StringIO):
            with self.assertRaisesRegex(z.Error,'--allow-small-target'):
                z.confirm_restore_capacity(plan,z.argparse.Namespace(dry_run=False,unattended=True))
            z.confirm_restore_capacity(plan,z.argparse.Namespace(dry_run=False,unattended=True,allow_small_target=True))
            z.confirm_restore_capacity(plan,z.argparse.Namespace(dry_run=True))
            z.confirm_restore_capacity({'capacity_warnings':[]},z.argparse.Namespace(dry_run=False))
        prompt.assert_not_called()

    def test_restore_flag_is_parsed(self):
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'restore') as restore:
            self.assertEqual(z.main(['restore','--allow-small-target','--dry-run'],restore_isolated=True),0)
        self.assertTrue(restore.call_args.args[0].allow_small_target)


class RestoreCompressionTests(unittest.TestCase):
    def test_default_is_lz4_before_receive(self):
        with patch.object(z,'run') as run:
            z.prepare_restore_compression({'name':'rpool','datasets':{'rpool':{}}},'target',{''})
        run.assert_called_once_with('zfs','set','compression=lz4','target')

    def test_existing_datasets_get_selected_settings_before_incremental_receive(self):
        pool={'name':'rpool','datasets':{
            'rpool':{'compression':{'value':'zstd','source':'local'}},
            'rpool/child':{'compression':{'value':'zstd','source':'inherited from rpool'}},
            'rpool/new':{'compression':{'value':'gzip','source':'local'}},
            'rpool@point':{'compression':{'value':'off'}}}}
        with patch.object(z,'run') as run:z.prepare_restore_compression(pool,'target',{'','/child'})
        self.assertEqual([c.args for c in run.call_args_list],[
            ('zfs','set','compression=lz4','target'),('zfs','set','compression=zstd','target'),
            ('zfs','inherit','compression','target/child')])


class ValidationTests(unittest.TestCase):
    def test_valid(self):self.assertIn('zfs/tank.zfs',z.validate(fixture()))

    def test_unsupported(self):
        for kind in ('ext4','xfs','btrfs','luks','lvm','unknown'):
            m=fixture();m['disk']['partitions'][0]['kind']=kind
            with self.assertRaises(z.Error):z.validate(m)

    def test_unknown_guid(self):
        m=fixture();m['disk']['partitions'][0]['type_guid']=str(uuid.uuid4())
        with self.assertRaises(z.Error):z.validate(m)

    def test_incremental_and_version(self):
        for key,value in [('backup_type','incremental'),('version',4)]:
            m=fixture();m[key]=value
            with self.assertRaises(z.Error):z.validate(m)

    def test_bad_paths(self):
        for path in ('../escape','/etc/passwd','a/../../b','a\nb','a\\b'):
            with self.assertRaises(z.Error):z.safe_rel(path)

    def test_topology(self):
        m=fixture();m['pools'][0]['topology']='mirror'
        with self.assertRaises(z.Error):z.validate(m)

    def test_encrypted_root(self):
        m=fixture();m['pools'][0]['datasets']['tank']['encryption']={'value':'aes-256-gcm'}
        with self.assertRaises(z.Error):z.validate(m)

    def test_fstab_unsupported(self):
        m=fixture();m['boot']['fstab']='/dev/sdb1 /data ext4 defaults 0 2'
        with self.assertRaises(z.Error):z.validate(m)

    def test_fstab_network_mounts_allowed(self):
        entries=(
            '//proxmox.localdomain/data /mnt/data cifs credentials=/etc/creds,x-systemd.automount,x-systemd.idle-timeout=60,nofail,uid=100000,gid=100000,file_mode=0777,dir_mode=0777,iocharset=utf8,vers=3.0 0 0',
            'server:/data /mnt/data nfs defaults 0 0',
            'server:/data /mnt/data nfs4 defaults 0 0',
        )
        for entry in entries:
            with self.subTest(entry=entry):
                m=fixture()
                m['boot']['fstab']='proc /proc proc defaults 0 0\n'+entry+'\n'
                z.validate(m)

    def test_unsafe_mount(self):
        m=fixture();m['mounts'][0]['target']='/../../etc'
        with self.assertRaises(z.Error):z.validate(m)

    def test_bad_hash_precedes_zstream(self):
        m=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);(base/'manifest.json').write_text(json.dumps(m))
            artifacts=z.validate(m)|{'manifest.json','disk/gpt.bin','disk/first-megabyte.bin'}
            (base/'SHA256SUMS').write_text(''.join('0'*64+'  '+x+'\n' for x in sorted(artifacts)))
            with patch.object(z.subprocess,'run') as run:
                with self.assertRaises(z.Error):z.verify_backup(base)
                run.assert_not_called()

    def test_cleanup_never_deletes_contents(self):
        with z.recovery_directory() as directory:
            (directory/'precious').write_text('keep')
        self.assertEqual((directory/'precious').read_text(),'keep')
        (directory/'precious').unlink();directory.rmdir()

    def test_confirmation_must_match(self):
        with patch.object(z,'node_for',return_value={'model':'Test SSD'}),patch('builtins.input',return_value='yes'):
            with self.assertRaises(z.Error):z.confirm('/dev/fake')

    def test_destruction_warning_contains_device_and_model_but_requires_only_destroy(self):
        device='/dev/disk/by-id/wwn-0x500a075103053544'
        with patch.object(z,'node_for',return_value={'model':'  Test SSD 120GB  '}), \
             patch('builtins.input',return_value='DESTROY') as prompt:
            z.confirm(device)
        prompt.assert_called_once_with(f'All data on {device} (model: Test SSD 120GB) will be destroyed.\nType DESTROY to confirm: ')

    def test_destruction_model_unavailable_is_explicit(self):
        self.assertEqual(z.destruction_target('/dev/test',{'model':None}),'/dev/test (model: unavailable)')

    def test_unattended_destruction_message_includes_model(self):
        with patch.object(z,'node_for',return_value={'model':'Test SSD'}), \
             patch('builtins.input') as prompt,patch('sys.stdout',new_callable=io.StringIO) as output:
            z.confirm('/dev/test',skip=True)
        self.assertIn('confirmed destruction of /dev/test (model: Test SSD)',output.getvalue())
        prompt.assert_not_called()

    def test_combined_captures_send_estimates(self):
        with patch.object(z.subprocess,'run',return_value=z.subprocess.CompletedProcess([],0,b'',b'size\t123\n')):
            self.assertEqual(z.run('zfs',combined=True),'size\t123\n')


class SourceSignatureTests(unittest.TestCase):
    def check(self, signatures):
        m=fixture()
        with patch.object(z, 'signatures', return_value=signatures):
            z.validate_source_signatures('/dev/source', m['disk'], {'tank': m['pools'][0]})

    def signature(self, relative, guid='123'):
        part=fixture()['disk']['partitions'][1]
        return dict(type='zfs_member', offset=hex(part['start_lba']*512+relative), uuid=guid)

    def test_gpt_only(self):
        self.check([dict(type='gpt'), dict(type='PMBR')])

    def test_partition_labels_visible_on_whole_disk(self):
        size=fixture()['disk']['partitions'][1]['size_bytes']
        for relative in (16384, 256*1024+16384, size-512*1024+16384, size-256*1024+16384):
            with self.subTest(relative=relative):
                self.check([dict(type='gpt'), self.signature(relative)])

    def test_rejects_wrong_pool_or_missing_identity(self):
        for guid in ('999', None):
            with self.subTest(guid=guid), self.assertRaises(z.Error):
                self.check([self.signature(16384, guid)])

    def test_rejects_signatures_outside_partition_labels(self):
        size=fixture()['disk']['partitions'][1]['size_bytes']
        for relative in (-16384, 1024*1024, size):
            with self.subTest(relative=relative), self.assertRaises(z.Error):
                self.check([self.signature(relative)])

    def test_rejects_other_filesystems(self):
        for kind in ('ext4', 'LVM2_member', 'vfat'):
            with self.subTest(kind=kind), self.assertRaises(z.Error):
                self.check([dict(type=kind, offset='0x1000')])


class PoolAshiftTests(unittest.TestCase):
    def test_reads_device_labels_without_pool_cache_lookup(self):
        labels = "LABEL 0\n    pool_guid: 123\n        ashift: 12\nLABEL 1\n    pool_guid: 123\n        ashift: 12\n"
        with patch.object(z, 'run', return_value=labels) as run:
            self.assertEqual(z.pool_ashift(['/dev/source3'], '123'), 12)
        run.assert_called_once_with('zdb', '-l', '/dev/source3')

    def test_rejects_wrong_or_missing_pool_identity(self):
        for labels in ('pool_guid: 456\nashift: 12\n', 'ashift: 12\n'):
            with self.subTest(labels=labels), patch.object(z, 'run', return_value=labels):
                with self.assertRaises(z.Error):
                    z.pool_ashift(['/dev/source3'], '123')

    def test_rejects_missing_invalid_or_conflicting_ashift(self):
        for values in ('', 'ashift: 0\n', 'ashift: 12\nashift: 9\n'):
            with self.subTest(values=values), patch.object(z, 'run', return_value='pool_guid: 123\n'+values):
                with self.assertRaises(z.Error):
                    z.pool_ashift(['/dev/source3'], '123')

    def test_label_read_failure_aborts(self):
        with patch.object(z, 'run', side_effect=z.Error('label read failed')):
            with self.assertRaises(z.Error):
                z.pool_ashift(['/dev/source3'], '123')


class GptTests(unittest.TestCase):
    def disk_image(self,path,hybrid=False,corrupt=False):
        sectors=8192;sector=512;entries=bytearray(128*128)
        struct.pack_into('<16s16sQQQ',entries,0,uuid.UUID(z.ESP).bytes_le,uuid.uuid4().bytes_le,2048,4095,0)
        entries[56:62]='EFI'.encode('utf-16-le')
        guid=uuid.uuid4().bytes_le
        def header(current,other,table):
            h=bytearray(512);h[:8]=b'EFI PART'
            struct.pack_into('<IIIIQQQQ16sQIII',h,8,0x10000,92,0,0,current,other,34,sectors-34,guid,table,128,128,zlib.crc32(entries))
            struct.pack_into('<I',h,16,zlib.crc32(h[:92]))
            return h
        with open(path,'wb') as f:
            f.truncate(sectors*sector);mbr=bytearray(512);mbr[510:]=b'\x55\xaa';mbr[450]=0xee
            if hybrid:mbr[466]=0x83
            f.write(mbr);f.write(header(1,sectors-1,2));f.write(entries)
            f.seek((sectors-33)*sector);f.write(entries);f.write(header(sectors-1,1,sectors-33))
            if corrupt:f.seek(1024);f.write(b'bad')
        return sectors*sector

    def test_reads_both_gpt_copies(self):
        with tempfile.NamedTemporaryFile() as f:
            size=self.disk_image(f.name);g=z.read_gpt(f.name,512,size)
            self.assertEqual(g['partitions'][0]['name'],'EFI')
            self.assertEqual(g['partitions'][0]['size_bytes'],z.MIB)

    def test_rejects_hybrid_and_crc_error(self):
        for kwargs in ({'hybrid':True},{'corrupt':True}):
            with tempfile.NamedTemporaryFile() as f:
                size=self.disk_image(f.name,**kwargs)
                with self.assertRaises(z.Error):z.read_gpt(f.name,512,size)


class RestoreBoundaryTests(unittest.TestCase):
    def invoke(self, m, size, dry=True):
        import argparse
        args=argparse.Namespace(backup='/backup',target='/dev/fake',dry_run=dry)
        m['storage']='native'
        n={'size':size,'log-sec':512,'phy-sec':4096,'model':'test'}
        with patch.object(z,'stable_device',side_effect=str), patch.object(z,'estimate_native_send',return_value=m['pools'][0]['used_bytes']), patch.object(z,'commands'), patch.object(z,'select_backup',return_value=Path('/backup')), patch.object(z,'verify_chain',return_value=[(Path('/backup'),m)]) as verification, \
             patch.object(z,'protected_path',return_value=set()), patch.object(z,'inventory',return_value={'blockdevices':[]}), \
             patch.object(z,'target_idle',return_value=n), \
             patch.object(z,'run',side_effect=lambda *a,**k:'-g' if a[:2]==('zpool','reguid') else ('zfs-kmod-2.3.0' if a==('zfs','version') else '')) as run, \
             patch.object(z,'create_layout') as write, \
             patch.object(z,'confirm') as confirm, patch('sys.stdout',new_callable=io.StringIO):
            try:
                z.restore_from_storage(args,Path("/backup"))
            finally:
                verification.assert_called_once_with(Path('/backup'),read_native=False)
                write.assert_not_called()
                confirm.assert_not_called()
                self.assertTrue(all(c.args[:2] in [('zpool','list'),('zpool','upgrade'),('zpool','reguid'),('zfs','version'),('unshare','--mount')] for c in run.call_args_list))

    def test_dry_run_no_writes(self):
        self.invoke(fixture(),500*z.GIB)

    def test_declining_estimated_shortfall_never_confirms_destruction_or_writes(self):
        with patch('builtins.input',return_value='n') as prompt,self.assertRaises(z.Error):
            self.invoke(fixture(),100*z.GIB,False)
        self.assertIn('estimated capacity shortfall',prompt.call_args.args[0])

    def test_dry_run_displays_estimated_shortfall_without_prompt(self):
        with patch('builtins.input') as prompt:self.invoke(fixture(),100*z.GIB)
        prompt.assert_not_called()

    def test_checksum_failure_never_inspects_or_writes_target(self):
        import argparse
        args=argparse.Namespace(backup='/backup')
        with patch.object(z,'commands'), patch.object(z,'select_backup',return_value=Path('/backup')), patch.object(z,'verify_chain',side_effect=z.Error('hash')), \
             patch.object(z,'target_idle') as inspect, patch.object(z,'create_layout') as write:
            with self.assertRaises(z.Error):z.restore_from_storage(args,Path("/backup"))
            inspect.assert_not_called();write.assert_not_called()


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        inventory=patch.object(z,'native_dataset_names',side_effect=lambda pool,remote=None:z.native_expected_names(pool))
        inventory.start();self.addCleanup(inventory.stop)

    def incremental(self):
        m=fixture();m.update(version=2,backup_type='incremental',parent='backup-20260914-183000',
                            parent_manifest_sha256='a'*64,base_snapshot=m['snapshot'],snapshot='baremetal-20260915-183000')
        return m

    def test_incremental_manifest(self):
        z.validate(self.incremental())

    def test_unsafe_parent(self):
        m=self.incremental();m['parent']='../backup-20260914-183000'
        with self.assertRaises(z.Error):z.validate(m)

    def test_send_flags(self):
        m=self.incremental();p=m['pools'][0];p['encrypted']=True
        self.assertEqual(z.replication_flags(p,m),['-R','-w','-I','tank@baremetal-20260914-183000'])
        m['backup_type']='full'
        self.assertEqual(z.replication_flags(p,m),['-R','-w'])

    def test_select_snapshot(self):
        a=fixture();b=self.incremental()
        with patch.object(z,'catalog',return_value=[(Path('/repo/a'),a),(Path('/repo/b'),b)]):
            self.assertEqual(z.select_backup('/repo',b['snapshot']),Path('/repo/b'))
            with self.assertRaises(z.Error):z.select_backup('/repo','missing')

    def test_full_does_not_need_parent(self):
        with patch.object(z,'catalog') as catalog:
            self.assertIsNone(z.incremental_parent('/repo',fixture(),full=True));catalog.assert_not_called()

    def test_chain_replay_order_and_hash_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);a=root/'backup-20260914-183000';b=root/'backup-20260915-183000'
            a.mkdir();b.mkdir();(a/'manifest.json').write_text('parent')
            full=fixture();inc=copy.deepcopy(full);inc.update(self.incremental())
            inc['disk']=full['disk'];inc['pools']=full['pools']
            inc['parent_manifest_sha256']=z.digest(a/'manifest.json')
            with patch.object(z,'verify_backup',side_effect=lambda p: full if p==a else inc):
                self.assertEqual([p for p,_ in z.verify_chain(b)],[a,b])
                (a/'manifest.json').write_text('changed')
                with self.assertRaises(z.Error):z.verify_chain(b)

    def test_missing_chain_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            b=Path(tmp)/'backup-20260915-183000';b.mkdir()
            with patch.object(z,'verify_backup',return_value=self.incremental()):
                with self.assertRaises(z.Error):z.verify_chain(b)

    def test_existing_imported_storage_ignores_whole_disk_signature_without_disk_writes(self):
        name='linux_os_backup_test'
        n={'path':'/dev/dest','type':'disk','fstype':'zfs_member','label':'rpool',
           'children':[{'path':'/dev/dest1','type':'part','fstype':'zfs_member','label':name}]}
        def read(*args,**kwargs):
            if args[0]=='blkid':return 'LABEL='+name+'\nUUID=123\n'
            if args[:2]==('zpool','list'):return name+'\n'
            if 'mountpoint' in args:return '/tmp\n'
            if args[0] in ('mount','umount'):return ''
            self.fail('Unexpected command: '+repr(args))
        with patch.object(z,'private_storage_namespace'),patch.object(z,'stable_device',side_effect=str), patch.object(z,'node_for',return_value=n), patch.object(z,'run',side_effect=read), \
             patch.object(z,'props',return_value={name:{'guid':{'value':'123'}}}), \
             patch.object(z,'pool_leaves',return_value=['/dev/dest1']), \
             patch.object(z,'create_layout') as destroy, patch.object(z,'confirm') as confirm:
            with z.existing_store('/dev/dest','/dev/source') as path:
                self.assertNotEqual(path,Path('/tmp'))
                self.assertTrue(path.is_dir())
            destroy.assert_not_called();confirm.assert_not_called()

    def test_no_common_source_snapshot_starts_full_generation(self):
        m=fixture();old=copy.deepcopy(m)
        for pool in old['pools']:
            for dataset in list(pool['datasets']):
                pool['datasets'][dataset+'@'+old['snapshot']]={'guid':{'value':'123'}}
        with patch.object(z,'catalog',return_value=[(Path('/repo/base'),old)]), \
             patch.object(z,'verify_chain'), patch.object(z,'run',return_value='456'):
            self.assertIsNone(z.incremental_parent('/repo',m))

    def test_new_datasets_allow_incremental_with_existing_base_checks(self):
        old=native_fixture();m=copy.deepcopy(old)
        for name in ('tank/new','tank/new/child','tank/new-volume'):
            m['pools'][0]['datasets'][name]={}
        with patch.object(z,'catalog',return_value=[(Path('/repo/base'),old)]), \
             patch.object(z,'verify_chain'),patch.object(z,'run',return_value='123') as run:
            parent,base=z.incremental_parent('/repo',m,native_only=True)
        self.assertEqual(parent,Path('/repo/base'))
        self.assertEqual(base,old)
        self.assertEqual({c.args[-1] for c in run.call_args_list},
                         {'tank@'+old['snapshot'],'tank/ROOT/ubuntu@'+old['snapshot']})
        m.update(backup_type='incremental',base_snapshot=base['snapshot'])
        self.assertEqual(z.replication_flags(m['pools'][0],m),['-R','-I','tank@'+base['snapshot']])

    def test_additions_do_not_bypass_replaced_base_check(self):
        old=native_fixture();m=copy.deepcopy(old)
        m['pools'][0]['datasets']['tank/new']={}
        with patch.object(z,'catalog',return_value=[(Path('/repo/base'),old)]), \
             patch.object(z,'verify_chain'),patch.object(z,'run',return_value='456'):
            self.assertIsNone(z.incremental_parent('/repo',m,native_only=True))

    def test_searches_older_backups_for_matching_snapshot_identity(self):
        old=native_fixture();new=copy.deepcopy(old)
        new['snapshot']='system-backup-20260915-120000'
        for pool in new['pools']:
            for name in list(pool['datasets']):
                if '@' not in name:
                    pool['datasets'][name+'@'+new['snapshot']]={'guid':{'value':'456'}}
        current=copy.deepcopy(new)
        candidates=[(Path('/repo/old'),old),(Path('/repo/new'),new)]
        for newest in ('','789'):
            def read(*args,**kwargs):
                return newest if args[-1].endswith('@'+new['snapshot']) else '123'
            with self.subTest(newest=newest),patch.object(z,'catalog',return_value=candidates), \
                 patch.object(z,'verify_chain') as verify,patch.object(z,'run',side_effect=read):
                self.assertEqual(z.incremental_parent('/repo',current,native_only=True),candidates[0])
                verify.assert_called_once_with(Path('/repo/old'),read_native=False)

    def test_explicit_missing_common_base_errors_instead_of_silently_full(self):
        old=native_fixture()
        with patch.object(z,'catalog',return_value=[(Path('/repo/base'),old)]), \
             patch.object(z,'run',return_value=''),patch.object(z,'verify_chain') as verify:
            with self.assertRaisesRegex(z.Error,'no usable common'):
                z.incremental_parent('/repo',copy.deepcopy(old),explicit=old['snapshot'],native_only=True)
            verify.assert_not_called()

    def test_common_base_must_work_for_both_pools(self):
        old=ubuntu_fixture();new=copy.deepcopy(old);new['snapshot']='system-backup-20260915-120000'
        for pool in new['pools']:
            for name in list(pool['datasets']):
                if '@' not in name:
                    pool['datasets'][name+'@'+new['snapshot']]={'guid':{'value':'999'}}
        def read(*args,**kwargs):
            snap=args[-1]
            if snap.endswith('@'+new['snapshot']):return '' if snap.startswith('rpool') else '999'
            pool=next(p for p in old['pools'] if snap.startswith(p['name']+'@') or snap.startswith(p['name']+'/'))
            return z.val(pool['datasets'][snap],'guid')
        with patch.object(z,'catalog',return_value=[(Path('/repo/old'),old),(Path('/repo/new'),new)]), \
             patch.object(z,'run',side_effect=read),patch.object(z,'verify_chain'):
            self.assertEqual(z.incremental_parent('/repo',copy.deepcopy(new),native_only=True)[0],Path('/repo/old'))

    def test_removed_dataset_is_retained_without_querying_its_missing_source(self):
        m=native_fixture();old=copy.deepcopy(m);old['pools'][0]['datasets']['tank/old']={}
        with patch.object(z,'catalog',return_value=[(Path('/repo/base'),old)]), \
             patch.object(z,'verify_chain'),patch.object(z,'run',return_value='123') as run, \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            parent,base=z.incremental_parent('/repo',m)
        self.assertEqual(parent,Path('/repo/base'))
        self.assertIn('Removed datasets retained only in earlier recovery points: tank/old',output.getvalue())
        self.assertTrue(all('tank/old@' not in c.args[-1] for c in run.call_args_list))

    def test_bios_partition_fixed_and_validated(self):
        m=fixture();bios=dict(number=1,kind='bios_boot',start_lba=34,end_lba=2047,
                size_bytes=2014*512,type_guid=z.BIOS,partuuid=str(uuid.uuid4()),name='',attributes=0,
                source_device='/dev/fake1',image='disk/bios-boot-1.bin')
        m['disk']['partitions'].insert(0,bios)
        self.assertIn(bios['image'],z.validate(m))
        plan=z.solve(m,500*z.GIB,512)
        self.assertEqual(plan['partitions'][0]['size_bytes'],2014*512)
        self.assertEqual(plan['partitions'][0]['kind'],'bios_boot')


class MultiHostTests(unittest.TestCase):
    def populate(self,root,host):
        directory=z.host_repository(root,host)
        backup=directory/'backup-20260914-183000';backup.mkdir()
        m=fixture();m.update(version=3,hostname=host)
        (backup/'manifest.json').write_text(json.dumps(m));(backup/'SHA256SUMS').write_text('')
        return directory,backup,m

    def test_host_isolation_and_cli_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            a,_,ma=self.populate(tmp,'proxmox2');b,_,mb=self.populate(tmp,'other-host')
            self.assertNotEqual(a,b)
            self.assertEqual(z.select_host_repository(tmp,'proxmox2'),a)
            self.assertEqual(z.select_host_repository(tmp,'other-host'),b)
            # Even identical source disk and pool GUIDs must not cross host chains.
            mb['disk']=ma['disk'];mb['pools']=ma['pools']
            self.assertNotEqual(z.chain_identity(ma),z.chain_identity(mb))

    def test_interactive_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.populate(tmp,'alpha');b,_,_=self.populate(tmp,'beta')
            with patch('builtins.input',return_value='2'):
                self.assertEqual(z.select_host_repository(tmp),b)

    def test_unknown_and_mismatched_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory,backup,_=self.populate(tmp,'alpha')
            for path in (tmp,directory,backup):
                with self.assertRaises(z.Error):z.select_host_repository(path,'other')

    def test_hostname_paths_rejected(self):
        for name in ('../escape','/absolute','','host/name','..','a\nb',None):
            with self.assertRaises(z.Error):z.valid_hostname(name)

    def test_symlink_host_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'hosts').symlink_to('/tmp')
            with self.assertRaises(z.Error):z.host_repository(tmp,'alpha')

    def test_direct_backup_without_hostname_remains_selectable(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp)/'manifest.json').write_text(json.dumps(fixture()))
            self.assertEqual(z.select_host_repository(tmp),Path(tmp))
            with self.assertRaises(z.Error):z.select_host_repository(tmp,'alpha')

    def test_version_three_requires_hostname(self):
        m=fixture();m['version']=3
        with self.assertRaises(z.Error):z.validate(m)
        m['hostname']='proxmox2';z.validate(m)


class PoolImportTests(unittest.TestCase):
    def test_stale_host_id_is_repaired_with_one_force_import(self):
        stale=('zpool','import','-N','-o','cachefile=none','-d','/search','123')
        forced=('zpool','import','-N','-f','-o','cachefile=none','-d','/search','123')
        with patch.object(z,'run',side_effect=[z.Error('zpool import: pool was previously in use from another system'), '']) as run, \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            z.import_backup_pool('/search','123','linux_os_backup_test')
        self.assertEqual([c.args for c in run.call_args_list],[stale,forced])
        self.assertIn('force-importing it once',output.getvalue())

    def test_non_stale_import_failure_is_not_forced(self):
        with patch.object(z,'run',side_effect=z.Error('permission denied')) as run:
            with self.assertRaisesRegex(z.Error,'permission denied'):
                z.import_backup_pool('/search','123','linux_os_backup_test')
        run.assert_called_once()

    def test_scoped_search_directory(self):
        with patch.object(z,'stable_device',side_effect=str), z.pool_search_directory('/dev/disk/by-id/wwn-backup-part1') as directory:
            path=Path(directory)
            self.assertEqual([x.name for x in path.iterdir()],['wwn-backup-part1'])
            self.assertTrue((path/'wwn-backup-part1').is_symlink())
            self.assertEqual((path/'wwn-backup-part1').readlink(),Path('/dev/disk/by-id/wwn-backup-part1'))
        self.assertFalse(path.exists())

    def test_discovery_checks_exact_guid(self):
        with patch.object(z,'run',return_value='  pool: linux_os_backup_test\n    id: 123\n state: ONLINE\n'):
            z.check_import_discovery('/search','123','linux_os_backup_test','/dev/dest1')
            with self.assertRaises(z.Error):z.check_import_discovery('/search','23','linux_os_backup_test','/dev/dest1')

    def test_no_discovered_pool_does_not_import_or_initialize(self):
        with patch.object(z,'run',return_value='no pools available to import') as run:
            with self.assertRaisesRegex(z.Error,'No disk initialization'):
                z.check_import_discovery('/search','123','linux_os_backup_test','/dev/dest1')
            self.assertEqual(run.call_count,2)
            self.assertEqual(run.call_args.args,('zpool','import','-D','-d','/search'))

    def test_destroyed_pool_goes_to_fresh_setup_without_recovery_prompt(self):
        with patch.object(z,'run',side_effect=['no pools available', ' pool: linux_os_backup_test\n id: 123\n']) as run, \
             patch('builtins.input') as prompt:
            with self.assertRaises(z.ReinitializeDestroyedPool):
                z.check_import_discovery('/search','123','linux_os_backup_test','/dev/dest1')
            prompt.assert_not_called()
            self.assertEqual([c.args for c in run.call_args_list],[
                ('zpool','import','-d','/search'),('zpool','import','-D','-d','/search')])

    def test_normal_pool_never_offers_recovery_or_reinitialization(self):
        with patch.object(z,'run',return_value=' id: 123\n') as run,patch('builtins.input') as prompt:
            self.assertEqual(z.check_import_discovery('/search','123','pool','/dev/dest1'),[])
            self.assertEqual(run.call_count,1);prompt.assert_not_called()

    def test_fresh_destroyed_pool_still_requires_destructive_confirmation(self):
        from unittest.mock import MagicMock
        fake=MagicMock();fake.expanduser.return_value=fake
        fake.exists.return_value=True;fake.stat.return_value.st_mode=z.stat.S_IFBLK
        fake.__str__.return_value='/dev/dest'
        def decline(device):
            self.assertIn('EXISTING TARGET LAYOUT: /dev/dest',output.getvalue())
            self.assertIn('old-backup',output.getvalue())
            raise z.Error('declined')
        with patch.object(z,'stable_device',side_effect=str), patch.object(z,'Path',return_value=fake), \
             patch.object(z,'node_for',return_value={'fstype':'zfs_member'}), \
             patch.object(z,'existing_store',side_effect=z.ReinitializeDestroyedPool()), \
             patch.object(z,'target_idle',return_value={'size':100*z.GIB,'log-sec':512,
                          'children':[{'path':'/dev/dest1','fstype':'zfs_member','label':'old-backup'}]}), \
             patch.object(z,'confirm',side_effect=decline) as confirm, \
             patch.object(z,'create_layout') as create,patch('sys.stdout',new_callable=io.StringIO) as output:
            with self.assertRaisesRegex(z.Error,'declined'):
                with z.backup_destination('/dev/dest','/dev/source',z.GIB):
                    self.fail('Must not yield before confirmation')
            confirm.assert_called_once_with('/dev/dest');create.assert_not_called()

    def test_exported_store_uses_directory_and_exports(self):
        name='linux_os_backup_test'; calls=[]; imported=False
        n={'path':'/dev/dest','type':'disk','children':[{'path':'/dev/dest1','type':'part','fstype':'zfs_member'}]}
        def run(*args,**kwargs):
            nonlocal imported
            calls.append(args)
            if args[:2]==('zfs','get') and 'mountpoint' in args:return '/tmp/previous-mountpoint\n'
            if args[0]=='mount':
                self.assertEqual(args[:5],('mount','-t','zfs','-o','zfsutil'))
            if args[0]=='blkid':return 'LABEL='+name+'\nUUID=123\n'
            if args[:2]==('zpool','list'):return name+'\n' if imported else ''
            if args[:3]==('zpool','import','-d'):
                self.assertTrue(Path(args[3]).is_dir())
                return '  pool: '+name+'\n    id: 123\n'
            if args[:3]==('zpool','import','-N'):
                imported=True
                self.assertTrue(Path(args[args.index('-d')+1]).is_dir())
                self.assertNotIn('-f',args)
            return ''
        with patch.object(z,'private_storage_namespace'),patch.object(z,'stable_device',side_effect=str), patch.object(z,'node_for',return_value=n),patch.object(z,'run',side_effect=run), \
             patch.object(z,'target_idle'),patch.object(z,'pool_leaves',return_value=['/dev/dest1']), \
             patch.object(z,'props',return_value={name:{'guid':{'value':'123'}}}), \
             patch.object(z,'create_layout') as destroy:
            with z.existing_store('/dev/dest','/dev/source') as path:
                self.assertTrue(path.is_dir())
            self.assertEqual(calls[-1],('zpool','export',name))
            destroy.assert_not_called()


class ZfsMountTests(unittest.TestCase):
    def test_managed_dataset_mount_uses_zfsutil_without_changing_properties(self):
        with patch.object(z,'run',side_effect=['/tmp/old-location\n','']) as run:
            z.mount_zfs_at('linux_os_backup_test',Path('/tmp/new-location'))
            self.assertEqual(run.call_args.args,('mount','-t','zfs','-o','zfsutil','linux_os_backup_test',Path('/tmp/new-location')))
            self.assertFalse(any(c.args[:2]==('zfs','set') for c in run.call_args_list))

    def test_legacy_mount_omits_zfsutil(self):
        with patch.object(z,'run',side_effect=['legacy\n','']) as run:
            z.mount_zfs_at('rpool/ROOT/host','/recovery')
            self.assertEqual(run.call_args.args,('mount','-t','zfs','rpool/ROOT/host','/recovery'))

    def test_second_invocation_selects_full_backup_as_incremental_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            m=fixture();m.update(version=3,hostname='proxmox2')
            for pool in m['pools']:
                for dataset in list(pool['datasets']):
                    pool['datasets'][dataset+'@'+m['snapshot']]={'guid':{'value':'123'}}
            repository=z.host_repository(tmp,'proxmox2')
            previous=repository/'backup-20260914-183000';previous.mkdir()
            (previous/'manifest.json').write_text(json.dumps(m));(previous/'SHA256SUMS').write_text('')
            current=copy.deepcopy(m);current['snapshot']='baremetal-20260915-183000'
            with patch.object(z,'verify_chain',return_value=[(previous,m)]),patch.object(z,'run',return_value='123\n'):
                parent,manifest=z.incremental_parent(repository,current)
            self.assertEqual(parent,previous)
            current.update(backup_type='incremental',base_snapshot=manifest['snapshot'])
            flags=z.replication_flags(current['pools'][0],current)
            self.assertEqual(flags,['-R','-I','tank@baremetal-20260914-183000'])


class EspBackupMountTests(unittest.TestCase):
    def mount(self,**changes):
        return dict({'target':'/boot/efi','source':'/dev/disk/by-id/efi-part1',
                     'fstype':'vfat','maj:min':'8:145','fsroot':'/'},**changes)

    def test_reuses_existing_mount_and_leaves_it_on_archive_failure(self):
        output=json.dumps({'filesystems':[self.mount()]})
        with patch.object(z,'node_for',return_value={'maj:min':'8:145'}), \
             patch.object(z,'run',return_value=output) as run,patch.object(z,'mounted') as mount:
            with self.assertRaisesRegex(z.Error,'archive failed'):
                with z.esp_backup_mount('/dev/sdj1') as path:
                    self.assertEqual(path,Path('/boot/efi'))
                    raise z.Error('archive failed')
            mount.assert_not_called()
            self.assertTrue(all(c.args[0]=='findmnt' for c in run.call_args_list))

    def test_unmounted_esp_uses_temporary_mount(self):
        with patch.object(z,'node_for',return_value={'maj:min':'8:145'}), \
             patch.object(z,'run',return_value=json.dumps({'filesystems':[]})), \
             patch.object(z,'mounted',return_value=z.contextlib.nullcontext(Path('/temporary'))) as mount:
            with z.esp_backup_mount('/dev/sdj1') as path:
                self.assertEqual(path,Path('/temporary'))
            mount.assert_called_once_with('/dev/sdj1')

    def test_rejects_overmounted_wrong_device_or_submount(self):
        for changes in ({'maj:min':'8:161'}, {'fsroot':'/EFI'},
                        {'children':[{'target':'/boot/efi/other'}]}):
            with self.subTest(changes=changes), \
                 patch.object(z,'node_for',return_value={'maj:min':'8:145'}), \
                 patch.object(z,'run',side_effect=[json.dumps({'filesystems':[self.mount()]}),
                                                   json.dumps({'filesystems':[self.mount(**changes)]})]), \
                 patch.object(z,'mounted') as mount:
                with self.assertRaises(z.Error):
                    with z.esp_backup_mount('/dev/sdj1'):self.fail('Unsafe mount yielded')
                mount.assert_not_called()


class RestoreTimelineTests(unittest.TestCase):
    def test_total_excludes_confirmation_wait_and_includes_cleanup(self):
        clock=[100.0]
        def restore(args):
            self.assertIsNone(z.DIAGNOSTIC_STARTED)
            clock[0]=700.0  # Selection, preflight and confirmation took ten minutes.
            z.start_restore_timeline()
            clock[0]=712.0  # Discard and restore.
            clock[0]+=3.0  # Cleanup before returning.
        with patch.object(z,'DIAGNOSTIC_STARTED',None), \
             patch.object(z.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(z.os,'geteuid',return_value=0), \
             patch.object(z,'restore',side_effect=restore), \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(z.main(['restore'],restore_isolated=True),0)
        self.assertRegex(output.getvalue(),r'\+00:00:00\] Restore started')
        self.assertRegex(output.getvalue(),r'\+00:00:15\] Restore total: 00:00:15')

    def test_no_total_before_confirmation(self):
        for error in (None,z.Error('Confirmation did not match; aborted')):
            with self.subTest(error=error),patch.object(z,'DIAGNOSTIC_STARTED',None), \
                 patch.object(z.os,'geteuid',return_value=0), \
                 patch.object(z,'restore',side_effect=error), \
                 patch('sys.stdout',new_callable=io.StringIO) as output, \
                 patch('sys.stderr',new_callable=io.StringIO):
                self.assertEqual(z.main(['restore'],restore_isolated=True),1 if error else 0)
                self.assertNotIn('Restore total:',output.getvalue())


class OperationRateTests(unittest.TestCase):
    def test_averages_include_all_pools_preparation_and_cleanup(self):
        for command in ('backup','restore'):
            clock=[100.0]
            @z.contextlib.contextmanager
            def cleanup():
                try:yield
                finally:clock[0]+=15
            def operation(args):
                if command=='restore':
                    clock[0]=700.0
                    z.start_restore_timeline()
                with cleanup():
                    clock[0]+=10  # Preparation.
                    clock[0]+=20  # First pool.
                    clock[0]+=30  # Second pool and verification.
                    totals=z.TransferTotals()
                    totals.stream=totals.uncompressed=50000000+100000000
                    totals.compressed=60000000
                    if command=='backup':totals.compression='zstd-5'
                    return totals
            with self.subTest(command=command),patch.object(z.time,'monotonic',side_effect=lambda:clock[0]), \
                 patch.object(z,'DIAGNOSTIC_STARTED',None),patch.object(z.os,'geteuid',return_value=0), \
                 patch.object(z,command,side_effect=operation),patch('sys.stdout',new_callable=io.StringIO) as output:
                self.assertEqual(z.main([command],restore_isolated=True),0)
            self.assertIn(command.capitalize()+' total: 00:01:15 | transferred: compressed 60.000 MB, uncompressed 150.000 MB '
                          '| average: compressed 0.800 MB/s, uncompressed 2.000 MB/s',output.getvalue())
            self.assertEqual(output.getvalue().count(' | transferred: '),1)
            self.assertRegex(output.getvalue().splitlines()[-1],r'^\[\d{4}-\d{2}-\d{2} .*\+00:01:15\]')
            if command=='backup':self.assertTrue(output.getvalue().rstrip().endswith(' | compression: zstd-5'))

    def test_failed_cleanup_and_dry_run_do_not_report_success_rates(self):
        for command in ('backup','restore'):
            for failed in (False,True):
                def operation(args):
                    if not failed:return
                    if command=='restore':z.start_restore_timeline()
                    z.report_destination_compression(command.capitalize(),[("rpool","target")],
                        execute=lambda *a:'target\t100\t0\t200\t2.00\n')
                    raise z.Error('cleanup failed')
                with self.subTest(command=command,failed=failed),patch.object(z,'DIAGNOSTIC_STARTED',None), \
                     patch.object(z.os,'geteuid',return_value=0),patch.object(z,command,side_effect=operation), \
                     patch('sys.stdout',new_callable=io.StringIO) as output,patch('sys.stderr',new_callable=io.StringIO):
                    self.assertEqual(z.main([command],restore_isolated=True),1 if failed else 0)
                self.assertNotIn(' | transferred: ',output.getvalue())

    def test_small_incremental_transfer_uses_actual_bytes_and_no_transfer_is_zero(self):
        for transferred,elapsed,expected in ((4000000,23,'0.174 MB/s'),(0,23,'0.000 MB/s'),(0,0,'unavailable')):
            with self.subTest(transferred=transferred,elapsed=elapsed),patch('sys.stdout',new_callable=io.StringIO) as output:
                totals=z.TransferTotals()
                totals.stream=totals.uncompressed=transferred
                totals.compressed=transferred//2
                z.report_operation_rates('Backup',totals,elapsed)
            self.assertIn(f'transferred: compressed {transferred/2/1000000:.3f} MB, uncompressed {transferred/1000000:.3f} MB',output.getvalue())
            self.assertIn('uncompressed '+expected,output.getvalue())
            self.assertNotIn('dataset sizes',output.getvalue())

    def test_missing_compressed_accounting_keeps_measured_uncompressed_total(self):
        totals=z.TransferTotals();totals.uncompressed=4000000;totals.compressed=None
        with patch('sys.stdout',new_callable=io.StringIO) as output:
            z.report_operation_rates('Backup',totals,20)
        self.assertIn('transferred: compressed unavailable, uncompressed 4.000 MB',output.getvalue())
        self.assertIn('average: compressed unavailable, uncompressed 0.200 MB/s',output.getvalue())


class TransferSpaceTests(unittest.TestCase):
    def inventory(self,heads,snapshots):
        rows={name:dict(type='filesystem',guid=str(i),txg=1,referenced=0,origin=origin)
              for i,(name,origin) in enumerate(heads.items(),1)}
        for name,guid,txg,size in snapshots:
            rows[name]=dict(type='snapshot',guid=str(guid),txg=txg,referenced=size,origin='-')
        return rows

    def output(self,rows):
        return ''.join('\t'.join(str(v) for v in (name,p['type'],p['guid'],p['txg'],p['referenced'],p['origin']))+'\n'
                       for name,p in rows.items())

    def test_full_replication_counts_each_dataset_and_intermediate_snapshot_once(self):
        after=self.inventory({'dest':'-','dest/vm':'-'},
                             [('dest@a',10,2,100),('dest@b',11,3,80),('dest/vm@b',12,3,500)])
        def run(*args):
            if args[:2]==('zfs','list'):return self.output(after)
            self.assertEqual(args,('zfs','get','-H','-p','-o','value','written@dest@a','dest@b'))
            return '40'
        # The later snapshot shrank, but 40 new bytes were still written.
        self.assertEqual(z.received_compressed_bytes('dest',{},run),640)

    def test_incremental_excludes_retained_data_and_counts_overwrites_and_new_datasets(self):
        before=self.inventory({'dest':'-'},[('dest@old',10,2,9000000),('dest@base',11,3,8000000)])
        after=self.inventory({'dest':'-','dest/new':'-'},
                             [('dest@base',11,3,8000000),('dest@middle',12,4,7000000),
                              ('dest@tip',13,5,6000000),('dest/new@tip',14,5,30)])
        calls=[]
        def run(*args):
            calls.append(args)
            if args[:2]==('zfs','list'):return self.output(after)
            return {'written@dest@base':'40','written@dest@middle':'50'}[args[-2]]
        self.assertEqual(z.received_compressed_bytes('dest',before,run),120)
        self.assertEqual([c[-1] for c in calls if c[1]=='get'],['dest@middle','dest@tip'])

    def test_clone_counts_only_blocks_written_since_origin(self):
        after=self.inventory({'dest':'-','dest/clone':'dest@base'},
                             [('dest@base',10,2,1000),('dest/clone@tip',11,3,1020)])
        def run(*args):
            if args[:2]==('zfs','list'):return self.output(after)
            self.assertEqual(args[-2:],('written@dest@base','dest/clone@tip'))
            return '20'
        self.assertEqual(z.received_compressed_bytes('dest',{},run),1020)

    def test_retained_snapshot_renames_are_recognized_by_identity(self):
        before=self.inventory({'dest':'-'},[('dest@base',10,2,9000000)])
        after=self.inventory({'dest':'-'},[('dest@renamed',10,2,9000000),('dest@tip',11,3,9000100)])
        def run(*args):
            if args[:2]==('zfs','list'):return self.output(after)
            self.assertEqual(args[-2:],('written@dest@renamed','dest@tip'))
            return '100'
        self.assertEqual(z.received_compressed_bytes('dest',before,run),100)

    def test_unknown_accounting_never_falls_back_to_net_growth_or_dataset_size(self):
        after=self.inventory({'dest':'-'},[('dest@a',10,2,100),('dest@b',11,3,200)])
        for result in ('-',z.Error('property unavailable'),OSError('failed')):
            with self.subTest(result=result),patch.object(z,'run',side_effect=[self.output(after),result]):
                self.assertIsNone(z.received_compressed_bytes('dest',{}))
        for output in ('','invalid','elsewhere\tfilesystem\t1\t1\t1\t-\n'):
            with self.subTest(output=output),patch.object(z,'run',return_value=output):
                self.assertIsNone(z.transfer_snapshot_inventory('dest'))

    def test_totals_sum_only_successful_run_streams_and_measured_allocations(self):
        totals=z.TransferTotals()
        with patch.object(z,'received_compressed_bytes',side_effect=[200,300]) as measure:
            totals.record(1234,'boot',{})
            totals.record(5678,'root',{})
            totals.record(0,'root',{})
        self.assertEqual((totals.stream,totals.uncompressed,totals.compressed),(6912,6912,500))
        self.assertEqual(measure.call_count,2)

    def test_partial_statistics_and_raw_encryption_are_not_reported_as_complete_totals(self):
        totals=z.TransferTotals()
        with patch.object(z,'received_compressed_bytes',side_effect=[200,None,300]):
            totals.record(1234,'boot',{})
            totals.record(2345,'root',{})
            totals.record(3456,'encrypted',{},raw=True)
        self.assertEqual(totals.stream,7035)
        self.assertIsNone(totals.compressed)
        self.assertIsNone(totals.uncompressed)


class ProgressTests(unittest.TestCase):
    def test_send_preserves_bytes_and_reports_throughput(self):
        with tempfile.TemporaryDirectory() as tmp,patch('sys.stderr',new_callable=io.StringIO) as output:
            path=Path(tmp)/'stream.zfs'
            z.stream_file([sys.executable,'-c',"import sys;sys.stdout.buffer.write(b'abc'*10000)"],path,30000)
            self.assertEqual(path.read_bytes(),b'abc'*10000)
            self.assertIn('MiB/s',output.getvalue());self.assertIn('ETA',output.getvalue())
            self.assertIn('complete',output.getvalue())
            final=output.getvalue().splitlines()[-1]
            self.assertIn('GiB transferred | estimated ',final)
            self.assertNotIn('%',final)
            self.assertRegex(final,r'\| avg [0-9.]+ MiB/s \|')
            self.assertEqual(final.count('MiB/s'),1)
            self.assertNotIn('(avg',final)

    def test_failed_sender_is_not_reported_complete(self):
        with tempfile.TemporaryDirectory() as tmp,patch('sys.stderr',new_callable=io.StringIO) as output:
            with self.assertRaises(z.Error):z.stream_file([sys.executable,'-c','raise SystemExit(1)'],Path(tmp)/'stream',100)
            self.assertIn('failed/interrupted',output.getvalue());self.assertNotIn(' | complete',output.getvalue())

    def test_receive_uses_shared_descriptor_offset(self):
        with tempfile.TemporaryDirectory() as tmp,patch('sys.stderr',new_callable=io.StringIO) as output:
            path=Path(tmp)/'stream';path.write_bytes(b'abc'*10000)
            def consume(*args,**kwargs):
                self.assertEqual(args,('zfs','receive','-u','-F','tank'))
                result=z.subprocess.run([sys.executable,'-c','import sys; assert len(sys.stdin.buffer.read())==30000'],stdin=kwargs['stdin'])
                self.assertEqual(result.returncode,0)
            with patch.object(z,'run',side_effect=consume):z.receive_file(path,'tank')
            self.assertIn('GiB transferred | estimated ',output.getvalue());self.assertIn('complete',output.getvalue())


def native_fixture():
    m=fixture();m.update(version=4,storage='native',hostname='proxmox2',storage_pool_guid='999',
        native_root='store/linux_os_backup/0123456789abcdef/proxmox2/baremetal-20260914-183000')
    for p in m['pools']:
        p['native_dataset']=m['native_root']+'/'+p['name'];p.pop('stream')
        for name in list(p['datasets']):
            p['datasets'][name+'@'+m['snapshot']]={'guid':{'value':'123'}}
    return m


class ForcedIncrementalBackupTests(unittest.TestCase):
    def test_parser_accepts_incremental_and_rejects_full_combination(self):
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'backup') as backup:
            self.assertEqual(z.main(['backup','--incremental']),0)
            self.assertTrue(backup.call_args.args[0].incremental)
            self.assertFalse(backup.call_args.args[0].full)
        with patch('sys.stderr',new_callable=io.StringIO),self.assertRaises(SystemExit) as error:
            z.main(['backup','--incremental','--full'])
        self.assertEqual(error.exception.code,2)

    def test_empty_or_wrong_source_repository_aborts(self):
        current=native_fixture();other=copy.deepcopy(current);other['pools'][0]['guid']='different'
        for catalog in ([],[(Path('/repo/other'),other)]):
            with self.subTest(catalog=catalog),patch.object(z,'catalog',return_value=catalog), \
                 patch.object(z,'run') as run,patch.object(z,'verify_chain') as verify:
                with self.assertRaisesRegex(z.Error,'no matching backup base'):
                    z.incremental_parent('/repo',current,native_only=True,required=True)
                run.assert_not_called();verify.assert_not_called()

    def test_missing_source_snapshot_or_changed_dataset_identity_aborts(self):
        for mismatch in ('snapshot','dataset'):
            old=native_fixture();current=copy.deepcopy(old)
            if mismatch=='dataset':
                old['pools'][0]['datasets']['tank']['guid']={'value':'100'}
                current['pools'][0]['datasets']['tank']['guid']={'value':'200'}
            with self.subTest(mismatch=mismatch),patch.object(z,'catalog',return_value=[(Path('/repo/base'),old)]), \
                 patch.object(z,'run',return_value='wrong' if mismatch=='snapshot' else '123'), \
                 patch.object(z,'verify_chain') as verify:
                with self.assertRaisesRegex(z.Error,'no usable matching base'):
                    z.incremental_parent('/repo',current,native_only=True,required=True)
                verify.assert_not_called()
                self.assertNotIn('replication_type',current['pools'][0])

    def test_matching_base_allows_new_datasets_but_not_reused_names(self):
        for collision in (False,True):
            old=native_fixture();current=copy.deepcopy(old)
            current['pools'][0]['datasets']['tank/new']={}
            native=old['pools'][0]['native_dataset']
            with self.subTest(collision=collision), \
                 patch.object(z,'catalog',return_value=[(Path('/repo/base'),old)]), \
                 patch.object(z,'run',return_value='123'),patch.object(z,'verify_chain'), \
                 patch.object(z,'native_dataset_names',return_value={native,native+'/new'} if collision else {native}):
                if collision:
                    with self.assertRaisesRegex(z.Error,'no usable matching base'):
                        z.incremental_parent('/repo',current,native_only=True,required=True)
                else:
                    self.assertEqual(z.incremental_parent('/repo',current,native_only=True,required=True),
                                     (Path('/repo/base'),old))
                    self.assertNotIn('replication_type',current['pools'][0])

    def test_blank_local_disk_never_reaches_destruction_prompt(self):
        fake=unittest.mock.MagicMock();fake.expanduser.return_value=fake
        fake.exists.return_value=True;fake.stat.return_value.st_mode=z.stat.S_IFBLK
        fake.__str__.return_value='/dev/backup'
        with patch.object(z,'Path',return_value=fake),patch.object(z,'stable_device',side_effect=str), \
             patch.object(z,'node_for',return_value={'type':'disk'}),patch.object(z,'confirm') as confirm, \
             patch.object(z,'create_layout') as erase,patch.object(z,'target_idle') as idle:
            with self.assertRaisesRegex(z.Error,'--incremental requires an existing usable backup disk'):
                with z.backup_destination('/dev/backup','/dev/source',z.GIB,require_existing=True):
                    self.fail('Blank target accepted')
            confirm.assert_not_called();erase.assert_not_called();idle.assert_not_called()

    def test_backup_storage_requires_existing_local_repository(self):
        args=z.argparse.Namespace(destination='/dev/backup',incremental=True)
        with patch.object(z,'backup_destination',return_value=z.contextlib.nullcontext('/repo')) as destination, \
             patch.object(z,'repository_lock',return_value=z.contextlib.nullcontext()):
            with z.backup_storage(args,{'disk':{'device':'/dev/source'}},123):pass
        destination.assert_called_once_with('/dev/backup','/dev/source',123,require_existing=True)


class NativeTests(unittest.TestCase):
    def setUp(self):
        inventory=patch.object(z,'native_dataset_names',side_effect=lambda pool,remote=None:z.native_expected_names(pool))
        inventory.start();self.addCleanup(inventory.stop)

    def test_file_backups_are_not_native_incremental_bases(self):
        old=fixture();current=copy.deepcopy(old)
        with patch.object(z,'catalog',return_value=[(Path('/repo/old'),old)]),patch.object(z,'verify_chain') as verify:
            self.assertIsNone(z.incremental_parent('/repo',current,native_only=True))
            verify.assert_not_called()

    def test_native_incremental_verifies_metadata_without_full_data_reread(self):
        old=native_fixture();current=copy.deepcopy(old)
        current['snapshot']='baremetal-20260916-000000'
        with patch.object(z,'catalog',return_value=[(Path('/repo/old'),old)]), \
             patch.object(z,'verify_chain') as verify,patch.object(z,'run',return_value='123'):
            parent,_=z.incremental_parent('/repo',current,native_only=True)
            self.assertEqual(parent,Path('/repo/old'))
            verify.assert_called_once_with(Path('/repo/old'),read_native=False)

    def test_mount_properties_are_restored_from_manifest(self):
        p=native_fixture()['pools'][0]
        p['datasets']['tank'].update(mountpoint={'value':'/tank','source':'local'},readonly={'value':'off','source':'default'},canmount={'value':'on','source':'default'})
        with patch.object(z,'run') as run:z.restore_mount_properties(p)
        calls=[c.args for c in run.call_args_list]
        self.assertIn(('zfs','set','mountpoint=/tank','tank'),calls)
        self.assertIn(('zfs','set','readonly=off','tank'),calls)
        self.assertFalse(any('@' in c[-1] for c in calls))

    def test_native_manifest_has_no_stream_artifacts(self):
        paths=z.validate(native_fixture())
        self.assertFalse(any(p.endswith('.zfs') for p in paths))
        self.assertIn('efi/esp-4.tar.zst',paths)

    def test_native_restore_does_not_require_incremental_parent_files(self):
        m=native_fixture();m['backup_type']='incremental'
        with patch.object(z,'verify_backup',return_value=m):
            chain=z.verify_chain('/repo/point')
        self.assertEqual(len(chain),1)

    def test_missing_native_snapshot_aborts(self):
        m=native_fixture()
        with patch.object(z,'zfs_repository',return_value=('store','999')),patch.object(z,'run',return_value='456'):
            with self.assertRaisesRegex(z.Error,'snapshot'):z.verify_native('/repo',m)

    def test_native_data_must_be_on_metadata_pool(self):
        with patch.object(z,'zfs_repository',return_value=('other','111')),patch.object(z,'run') as run:
            with self.assertRaises(z.Error):z.verify_native('/repo',native_fixture())
            run.assert_not_called()

    def test_restore_capacity_uses_full_snapshot_not_increment_size(self):
        m=native_fixture()
        with patch.object(z,'zfs_repository',return_value=('store','999')), \
             patch.object(z,'run',return_value='123'),patch.object(z,'pipe_transfer',return_value=100*z.GIB):
            z.verify_native('/repo',m,read_streams=True)
        self.assertTrue(all(p['estimated_send_bytes']==100*z.GIB for p in m['pools']))

    def test_readonly_unmounted_receive_never_forces_rollback(self):
        command=z.native_receive('store/native/tank')
        self.assertIn('-u',command);self.assertNotIn('-F',command)
        self.assertIn('readonly=on',command);self.assertIn('canmount=off',command)
        for prop in ('mountpoint','sharenfs','sharesmb','volmode'):
            self.assertEqual(command[command.index(prop)-1],'-x')

    def test_selected_native_restore_is_full_send(self):
        m=native_fixture();p=m['pools'][0];p['encrypted']=True
        command=z.native_send(p,'baremetal-20260910-000000')
        self.assertIn('-w',command);self.assertNotIn('-I',command);self.assertNotIn('-i',command)
        self.assertTrue(command[-1].endswith('@baremetal-20260910-000000'))

    def test_non_zfs_repository_rejected_before_zfs_commands(self):
        with patch.object(z,'run',return_value=json.dumps({'filesystems':[{'fstype':'ext4','source':'/dev/dest1'}]})) as run:
            with self.assertRaises(z.Error):z.zfs_repository('/repo')
            self.assertEqual(run.call_count,1)

    def test_native_pipeline_preserves_bytes_without_stream_file(self):
        with tempfile.TemporaryDirectory() as tmp,patch('sys.stderr',new_callable=io.StringIO) as output:
            count=z.pipe_transfer([sys.executable,'-c',"import sys;sys.stdout.buffer.write(b'abc'*1000000)"],
                [sys.executable,'-c',"import sys;assert sys.stdin.buffer.read()==b'abc'*1000000"],3000000,'test')
            self.assertEqual(count,3000000);self.assertEqual(list(Path(tmp).iterdir()),[])
        self.assertEqual(len(output.getvalue().splitlines()),1)
        self.assertIn(' | complete',output.getvalue())
        self.assertRegex(output.getvalue(),r'^\[\d{4}-\d{2}-\d{2} .*\] test:')

    def test_native_pipeline_propagates_sender_failure(self):
        with patch('sys.stderr',new_callable=io.StringIO) as log:
            with self.assertRaises(z.Error):
                z.pipe_transfer([sys.executable,'-c','raise SystemExit(3)'],
                    [sys.executable,'-c','import sys;sys.stdin.buffer.read()'],100,'test')
            self.assertNotIn(' | complete',log.getvalue())

    def test_native_pipeline_reports_bytes_to_parent_without_own_progress(self):
        counts=[]
        with patch('sys.stderr',new_callable=io.StringIO) as log,patch('sys.stdout',new_callable=io.StringIO) as output:
            size=z.pipe_transfer([sys.executable,'-c',"import sys;sys.stdout.buffer.write(b'x'*2500000)"],
                [sys.executable,'-c','import sys;sys.stdin.buffer.read()'],2500000,'child',on_bytes=counts.append)
        self.assertEqual(size,2500000)
        self.assertEqual(sum(counts),size)
        self.assertGreater(len(counts),1)
        self.assertEqual(log.getvalue(),'')
        self.assertEqual(output.getvalue(),'')

    def test_native_pipeline_propagates_receiver_failure(self):
        with patch('sys.stderr',new_callable=io.StringIO):
            with self.assertRaises((z.Error,BrokenPipeError)):
                z.pipe_transfer([sys.executable,'-c',"print('data')"],
                    [sys.executable,'-c','import sys;sys.stdin.buffer.read();raise SystemExit(3)'],100,'test')

    def test_newer_uncommitted_tip_requires_full(self):
        m=native_fixture();p=m['pools'][0]
        with patch.object(z,'run',side_effect=['-','-',p['native_dataset']+'@baremetal-20260916-000000']):
            with self.assertRaises(z.Error):z.assert_native_tip(p,m['snapshot'])

    def test_pending_receive_requires_full(self):
        with patch.object(z,'run',side_effect=['-','resume-token']) as run:
            with self.assertRaisesRegex(z.Error,'Incomplete native receive.*--full'):
                z.assert_native_tip(native_fixture()['pools'][0],'base')
        self.assertEqual(run.call_count,2)
        self.assertTrue(all(c.args[:2]==('zfs','get') for c in run.call_args_list))

    def test_pending_backup_error_identifies_marker_without_clearing_it(self):
        pool=native_fixture()['pools'][0];pending='system-backup-20260917-000000'
        with patch.object(z,'run',return_value=pending) as run:
            with self.assertRaisesRegex(z.Error,'incomplete backup') as error:
                z.assert_native_tip(pool,'base')
        self.assertIn(pool['native_dataset'],str(error.exception))
        self.assertIn(pending,str(error.exception))
        self.assertIn('automatic backup branches are unsupported',str(error.exception))
        self.assertIn('--full',str(error.exception))
        run.assert_called_once_with('zfs','get','-H','-o','value','org.linux-os-backup:pending',
                                    pool['native_dataset'].rsplit('/',1)[0])

    def test_existing_unmanaged_namespace_is_not_modified(self):
        m=native_fixture()
        with patch.object(z,'run',side_effect=['store/linux_os_backup\n','-']) as run:
            with self.assertRaises(z.Error):z.ensure_native_parent(m['native_root'])
            self.assertFalse(any(c.args[:2]==('zfs','create') for c in run.call_args_list))


class UnattendedBackupTests(unittest.TestCase):
    def setUp(self):
        prompt=patch('builtins.input',side_effect=AssertionError('Unattended backup prompted'))
        prompt.start();self.addCleanup(prompt.stop)

    def disk(self,path='/dev/backup',label='linux_os_backup_test'):
        return dict(type='disk',path=path,size=100*z.GIB,children=[
            dict(type='part',path=path+'1',fstype='zfs_member',label=label)])

    def test_single_backup_with_no_other_eligible_targets_runs_without_prompt(self):
        backup=self.disk(label='baremetal_store_legacy')
        disks=[self.disk('/dev/source'),self.disk('/dev/zd0'),self.disk('/dev/mounted','data'),backup]
        def idle(device,forbidden):
            if device=='/dev/mounted':raise z.Error('mounted')
        with patch.object(z,'inventory',return_value={'blockdevices':disks}),patch.object(z,'target_idle',side_effect=idle):
            self.assertEqual(z.choose_unattended_backup_destination('/dev/source'),'/dev/backup')

    def test_blank_or_data_disk_alongside_backup_requires_error(self):
        for other in (dict(type='disk',path='/dev/blank',size=z.GIB),self.disk('/dev/data','ordinary-data')):
            with self.subTest(other=other), \
                 patch.object(z,'inventory',return_value={'blockdevices':[self.disk(),other]}), \
                 patch.object(z,'target_idle'),patch.object(z,'signatures',return_value=[]), \
                 self.assertRaisesRegex(z.Error,'would require a prompt'):
                z.choose_unattended_backup_destination('/dev/source')

    def test_zero_or_multiple_existing_backups_fail(self):
        for disks in ([],[dict(type='disk',path='/dev/blank',size=z.GIB)],
                      [self.disk('/dev/a'),self.disk('/dev/b')]):
            with self.subTest(disks=disks),patch.object(z,'inventory',return_value={'blockdevices':disks}), \
                 patch.object(z,'target_idle'),patch.object(z,'signatures',return_value=[]), \
                 self.assertRaisesRegex(z.Error,'no available|would require a prompt'):
                z.choose_unattended_backup_destination('/dev/source')

    def test_explicit_existing_disk_is_reused_without_erasure(self):
        self.destination(existing=True)

    def test_blank_or_destroyed_disk_is_never_initialized_even_when_explicit(self):
        self.destination(existing=False)
        self.destination(existing=True,destroyed=True)

    def destination(self,existing,destroyed=False):
        from unittest.mock import MagicMock
        fake=MagicMock();fake.expanduser.return_value=fake
        fake.exists.return_value=True;fake.stat.return_value.st_mode=z.stat.S_IFBLK
        fake.__str__.return_value='/dev/backup'
        with patch.object(z,'stable_device',side_effect=str), patch.object(z,'Path',return_value=fake), \
             patch.object(z,'node_for',return_value=self.disk() if existing else {'type':'disk'}), \
             patch.object(z,'existing_store',side_effect=z.ReinitializeDestroyedPool() if destroyed else None,
                          return_value=z.contextlib.nullcontext('/repo')) as store, \
             patch.object(z,'confirm') as confirm,patch.object(z,'create_layout') as erase, \
             patch.object(z,'run') as run:
            if existing and not destroyed:
                with z.backup_destination('/dev/backup','/dev/source',z.GIB,unattended=True) as repo:
                    self.assertEqual(repo,'/repo')
                store.assert_called_once_with('/dev/backup','/dev/source')
            else:
                with self.assertRaisesRegex(z.Error,'will not initialize or erase'):
                    with z.backup_destination('/dev/backup','/dev/source',z.GIB,unattended=True):
                        self.fail('Unsafe target accepted')
            confirm.assert_not_called();erase.assert_not_called();run.assert_not_called()

    def test_backup_storage_passes_no_erase_policy_for_selected_and_explicit_disks(self):
        for explicit in (None,'/dev/explicit'):
            args=z.argparse.Namespace(destination=explicit,unattended=True)
            with patch.object(z,'choose_unattended_backup_destination',return_value='/dev/backup') as choose, \
                 patch.object(z,'choose_destination',side_effect=AssertionError('Interactive selection')), \
                 patch.object(z,'backup_destination',return_value=z.contextlib.nullcontext('/repo')) as destination, \
                 patch.object(z,'repository_lock',return_value=z.contextlib.nullcontext()):
                with z.backup_storage(args,{'disk':{'device':'/dev/source'}},123) as pair:
                    self.assertEqual(pair,('/repo',None))
            destination.assert_called_once_with(explicit or '/dev/backup','/dev/source',123,unattended=True)
            self.assertEqual(choose.call_count,0 if explicit else 1)

    def test_flag_is_backup_only_and_failure_returns_nonzero(self):
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'backup') as backup:
            self.assertEqual(z.main(['backup','--unattended']),0)
            self.assertTrue(backup.call_args.args[0].unattended)
        with patch.object(z.os,'geteuid',return_value=0), \
             patch.object(z,'backup',side_effect=lambda a:z.choose_unattended_backup_destination('/dev/source')), \
             patch.object(z,'inventory',return_value={'blockdevices':[]}),patch('sys.stderr',new_callable=io.StringIO):
            self.assertEqual(z.main(['backup','--unattended']),1)
        for command in ('verify','snapshots','clone-backup'):
            with self.subTest(command=command),patch('sys.stderr',new_callable=io.StringIO),self.assertRaises(SystemExit) as exc:
                z.main([command,'--unattended'])
            self.assertEqual(exc.exception.code,2)


class DestinationSelectionTests(unittest.TestCase):
    def test_attach_disk_then_refresh_for_backup_and_restore(self):
        disk={'type':'disk','path':'/dev/new','size':100*z.GIB}
        for backup in (False,True):
            with self.subTest(backup=backup), \
                 patch.object(z,'inventory',side_effect=[{'blockdevices':[]},{'blockdevices':[disk]}]) as inventory, \
                 patch.object(z,'target_idle') as idle,patch.object(z,'signatures',return_value=[]), \
                 patch('builtins.input',side_effect=['','']) as prompt,patch('sys.stdout',new_callable=io.StringIO) as output:
                self.assertEqual(z.choose_destination('Destination',allow_path=backup),'/dev/new')
            self.assertEqual(inventory.call_count,2)
            idle.assert_called_once_with('/dev/new',())
            self.assertIn('Attach a disk or unmount',output.getvalue())
            self.assertIn('Enter to refresh',prompt.call_args_list[0].args[0])

    def test_refresh_rechecks_busy_disks_and_keeps_protected_disks_excluded(self):
        disks=[{'type':'disk','path':p,'size':100*z.GIB} for p in ('/dev/source','/dev/busy')]
        with patch.object(z,'inventory',return_value={'blockdevices':disks}) as inventory, \
             patch.object(z,'target_idle',side_effect=[z.Error('mounted'),None]) as idle, \
             patch.object(z,'signatures',return_value=[]),patch('builtins.input',side_effect=['','']), \
             patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(z.choose_destination('Restore',['/dev/source']),'/dev/busy')
        self.assertEqual(inventory.call_count,2)
        self.assertEqual([c.args for c in idle.call_args_list],
                         [('/dev/busy',['/dev/source']),('/dev/busy',['/dev/source'])])

    def test_repeated_empty_scans_and_invalid_input_wait_until_quit(self):
        with patch.object(z,'inventory',return_value={'blockdevices':[]}) as inventory, \
             patch('builtins.input',side_effect=['bad','/dev/unknown','',' Q ']) as prompt, \
             patch.object(z,'run') as run,patch('sys.stdout',new_callable=io.StringIO), \
             self.assertRaises(z.Cancelled):
            z.choose_destination('Restore')
        self.assertEqual(inventory.call_count,2)
        self.assertEqual(prompt.call_count,4)
        run.assert_not_called()

    def test_quit_exits_command_cleanly_without_confirmation(self):
        def select(args):z.choose_destination('Destination')
        for command in ('backup','restore','clone-backup'):
            handler={'backup':'backup','restore':'restore','clone-backup':'clone_backup'}[command]
            with self.subTest(command=command),patch.object(z.os,'geteuid',return_value=0), \
                 patch.object(z,handler,side_effect=select),patch.object(z,'inventory',return_value={'blockdevices':[]}), \
                 patch('builtins.input',return_value='q'),patch.object(z,'confirm') as confirm, \
                 patch('sys.stdout',new_callable=io.StringIO) as output:
                self.assertEqual(z.main([command],restore_isolated=True),0)
            confirm.assert_not_called()
            self.assertIn('Destination selection cancelled.',output.getvalue())

    def test_zvols_do_not_prevent_automatic_physical_backup_selection(self):
        disk={'type':'disk','path':'/dev/sdb','size':100*z.GIB,
              'model':'PSSD T9','serial':'T9-123','tran':'usb','children':[
                  {'fstype':'zfs_member','label':'linux_os_backup_test'}]}
        virtual=[{'type':'disk','path':'/dev/'+name,'size':size}
                 for name,size in [('zd0',z.MIB),('zd16',96*z.GIB)]]
        with patch.object(z,'inventory',return_value={'blockdevices':virtual+[disk]}), \
             patch.object(z,'target_idle') as idle, patch('builtins.input') as prompt, \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(z.choose_destination('Backup',allow_path=True),'/dev/sdb')
            prompt.assert_not_called()
            idle.assert_called_once_with('/dev/sdb',())
            self.assertNotIn('/dev/zd',output.getvalue())
            self.assertIn('model: PSSD T9',output.getvalue())
            self.assertIn('serial: T9-123',output.getvalue())
            self.assertIn('connection: usb',output.getvalue())

    def test_explicit_zvol_target_rejected_before_device_access(self):
        with patch.object(z,'node_for',return_value={'path':'/dev/zd16','type':'disk'}), \
             patch.object(z.os,'stat') as stat:
            with self.assertRaisesRegex(z.Error,'virtual disk'):
                z.target_idle('/dev/zd16')
            stat.assert_not_called()

    def test_zvol_kernel_name_detects_alias(self):
        self.assertTrue(z.is_zvol({'path':'/dev/zvol/rpool/vm-disk','kname':'zd16'}))
        self.assertFalse(z.is_zvol({'path':'/dev/sdb','kname':'sdb'}))

    def test_only_backup_disk_skips_prompt(self):
        disk={'type':'disk','path':'/dev/backup','size':100*z.GIB,'children':[
            {'fstype':'zfs_member','label':'linux_os_backup_test'}]}
        with patch.object(z,'inventory',return_value={'blockdevices':[disk]}), \
             patch.object(z,'target_idle'),patch('builtins.input') as prompt:
            self.assertEqual(z.choose_destination('Backup',allow_path=True),'/dev/backup')
            prompt.assert_not_called()

    def test_restore_still_prompts_for_single_disk(self):
        disk={'type':'disk','path':'/dev/backup','size':100*z.GIB,'children':[
            {'fstype':'zfs_member','label':'linux_os_backup_test'}]}
        with patch.object(z,'inventory',return_value={'blockdevices':[disk]}), \
             patch.object(z,'target_idle'),patch('builtins.input',return_value='1') as prompt:
            self.assertEqual(z.choose_destination('Restore'),'/dev/backup')
            prompt.assert_called_once()

    def test_blank_disk_alongside_backup_disk_requires_prompt(self):
        backup={'type':'disk','path':'/dev/backup','size':100*z.GIB,'children':[
            {'fstype':'zfs_member','label':'linux_os_backup_test'}]}
        blank={'type':'disk','path':'/dev/blank','size':100*z.GIB}
        with patch.object(z,'inventory',return_value={'blockdevices':[backup,blank]}), \
             patch.object(z,'target_idle'),patch.object(z,'signatures',return_value=[]), \
             patch('builtins.input',return_value='2') as prompt:
            self.assertEqual(z.choose_destination('Backup',allow_path=True),'/dev/backup')
            prompt.assert_called_once()
            self.assertIn('[2]',prompt.call_args.args[0])

    def test_multiple_backup_disks_still_prompt(self):
        disks=[{'type':'disk','path':'/dev/'+name,'size':100*z.GIB,'children':[
            {'fstype':'zfs_member','label':'linux_os_backup_'+name}]} for name in ('a','b')]
        with patch.object(z,'inventory',return_value={'blockdevices':disks}), \
             patch.object(z,'target_idle'),patch('builtins.input',return_value='2') as prompt:
            self.assertEqual(z.choose_destination('Backup',allow_path=True),'/dev/b')
            prompt.assert_called_once()


class SnapshotManagementTests(unittest.TestCase):
    def test_group_size_counts_independent_copies_without_duplicate_children(self):
        groups=[dict(name='snap',snapshots=[(d+'@snap','123') for d in
                 ('store/g1/rpool','store/g1/rpool/ROOT','store/g2/rpool')])]
        with patch.object(z,'run',side_effect=[f'size\t{z.GIB}',f'size\t{z.GIB}',f'size\t{3*z.GIB}']) as estimate:
            z.estimate_snapshot_groups(groups)
        self.assertEqual(groups[0]['restore_bytes'],5*z.GIB)
        self.assertEqual([c.args[-1] for c in estimate.call_args_list],
                         ['store/g1/rpool@snap','store/g1/rpool/ROOT@snap','store/g2/rpool@snap'])
        self.assertTrue(all(c.args[:-1]==('zfs','send','-n','-P') for c in estimate.call_args_list))

    def test_failed_group_estimate_is_unavailable(self):
        groups=self.groups()
        with patch.object(z,'run',side_effect=z.Error('missing snapshot')):
            z.estimate_snapshot_groups(groups)
        self.assertTrue(all(g['restore_bytes'] is None for g in groups))

    def test_source_and_backup_listings_use_identical_uncompressed_snapshot_estimates(self):
        totals=[];commands=[]
        for backup,root in ((False,'rpool'),(True,'store/generation/rpool')):
            group=dict(name='point',created=1,snapshots=[(root+'@point','1'),(root+'/vm@point','2')])
            with patch.object(z,'snapshot_groups',return_value=[group]), \
                 patch.object(z,'run',side_effect=['size\t100','size\t200',
                    f'{root}@point\t{z.GIB}\t{2*z.GIB}\n{root}/vm@point\t{2*z.GIB}\t{3*z.GIB}\n']) as run, \
                 patch('sys.stdout',new_callable=io.StringIO) as output:
                z.manage_snapshot_list(root,range_text='',backup=backup)
            totals.append(group['restore_bytes'])
            commands.append([c.args[:-1] for c in run.call_args_list if c.args[:2]==('zfs','send')])
            self.assertTrue(all(c.args[-1].endswith('@point') for c in run.call_args_list))
            self.assertIn('Uncompressed ZFS stream:',output.getvalue())
            self.assertIn('datasets: compressed 3.000 GiB, uncompressed 5.000 GiB',output.getvalue())
            self.assertIn('older snapshot history is excluded',output.getvalue())
        self.assertEqual(totals,[300,300])
        self.assertEqual(commands[0],commands[1])
        self.assertEqual(commands[0],[('zfs','send','-n','-P')]*2)

    def test_bad_estimate_does_not_display_partial_group_size(self):
        group=self.groups()[0]
        with patch.object(z,'run',side_effect=['size\t100','invalid estimate']):
            z.estimate_snapshot_groups([group])
        self.assertIsNone(group['restore_bytes'])

    def test_size_visible_in_listing_and_purge_preview(self):
        groups=self.groups()[:1]
        with patch.object(z,'snapshot_groups',return_value=groups), \
             patch.object(z,'run',return_value=f'size\t{2*z.GIB}\n'), \
             patch.object(z,'inspect_purge'),patch('builtins.input') as prompt, \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            z.manage_snapshot_list('rpool','1-1',dry_run=True)
        self.assertEqual(output.getvalue().count('Uncompressed ZFS stream: ~4.000 GiB'),2)
        self.assertIn('not space that deletion will reclaim',output.getvalue())
        prompt.assert_not_called()

    def groups(self):
        return [dict(name='snap'+str(i),created=i,snapshots=[('rpool@snap'+str(i),'123'),('rpool/ROOT@snap'+str(i),'456')]) for i in range(1,6)]

    def read(self,*args,**kwargs):
        if args[:2]==('zfs','get'):
            prop=args[-2]
            if prop=='guid':return '456' if '/ROOT@' in args[-1] else '123'
            if prop=='clones':return '-'
            if prop=='defer_destroy':return 'off'
        return ''

    def test_unifies_snapshot_names_across_datasets_and_sorts(self):
        raw='rpool/ROOT@new\t200\t2\nrpool@old\t100\t3\nrpool@new\t200\t4\n'
        with patch.object(z,'run',return_value=raw):groups=z.snapshot_groups('rpool')
        self.assertEqual([g['name'] for g in groups],['old','new'])
        self.assertEqual(len(groups[1]['snapshots']),2)

    def test_rejects_outside_namespace(self):
        with patch.object(z,'run',return_value='other@x\t100\t1'):
            with self.assertRaises(z.Error):z.snapshot_groups('rpool')

    def test_numbered_range_is_inclusive(self):
        first,last=z.purge_range('1-3',5)
        self.assertEqual([g['name'] for g in self.groups()[first:last]],['snap1','snap2','snap3'])
        self.assertEqual(z.purge_range('5-5',5),(4,5))

    def test_bad_ranges_cannot_purge(self):
        for value in ('','1','0-3','3-1','1-6','-1-3','1-3,5','all'):
            with self.assertRaises(z.Error):z.purge_range(value,5)

    def test_dry_run_never_modifies_snapshots_or_prompts_confirmation(self):
        with patch.object(z,'run',side_effect=self.read) as run,patch('builtins.input') as prompt:
            z.purge_snapshots(self.groups(),'1-3',dry_run=True)
            self.assertTrue(all(c.args[1] in ('get','holds') for c in run.call_args_list))
            prompt.assert_not_called()

    def test_source_purge_deletes_exact_selected_names_only(self):
        with patch.object(z,'run',side_effect=self.read) as run,patch('builtins.input',return_value='PURGE 1-3'):
            z.purge_snapshots(self.groups(),'1-3')
            deleted=[c.args[-1] for c in run.call_args_list if c.args[:2]==('zfs','destroy')]
            self.assertEqual(set(deleted),{name for g in self.groups()[:3] for name,_ in g['snapshots']})
            self.assertFalse(any('-r' in c.args or '-R' in c.args for c in run.call_args_list))

    def test_external_intermediate_warning_precedes_confirmation_for_both_scopes(self):
        names=['system-backup-20260914-120000','manual-before-upgrade','system-backup-20260915-120000']
        groups=[dict(name=name,created=i,snapshots=[('rpool@'+name,'123')]) for i,name in enumerate(names)]
        for backup in (False,True):
            with self.subTest(backup=backup),patch.object(z,'inspect_purge'), \
                 patch.object(z,'retire_recovery_points',return_value=[]),patch.object(z,'run') as run, \
                 patch('sys.stdout',new_callable=io.StringIO) as output:
                def cancel(prompt):
                    self.assertIn('WARNING: Deleting externally named snapshot manual-before-upgrade',output.getvalue())
                    self.assertIn('intermediate snapshot between backup checkpoints',output.getvalue())
                    return 'no'
                with patch('builtins.input',side_effect=cancel),self.assertRaisesRegex(z.Error,'cancelled'):
                    z.purge_snapshots(groups,'2-2',backup=backup)
                run.assert_not_called()

    def test_intermediate_warning_requires_checkpoints_on_same_dataset(self):
        groups=[dict(name='system-backup-20260914-120000',created=1,snapshots=[('other@base','123')]),
                dict(name='manual',created=2,snapshots=[('rpool@manual','123')]),
                dict(name='system-backup-20260915-120000',created=3,snapshots=[('other@tip','123')])]
        with patch.object(z,'inspect_purge'),patch('sys.stdout',new_callable=io.StringIO) as output:
            z.purge_snapshots(groups,'2-2',dry_run=True)
        self.assertIn('WARNING: Deleting externally named snapshot manual',output.getvalue())
        self.assertNotIn('intermediate snapshot between',output.getvalue())

    def test_purging_only_tool_checkpoints_has_no_external_warning(self):
        groups=[dict(name='system-backup-20260914-120000',created=1,snapshots=[('rpool@base','123')])]
        with patch.object(z,'inspect_purge'),patch('sys.stdout',new_callable=io.StringIO) as output:
            z.purge_snapshots(groups,'1-1',dry_run=True)
        self.assertNotIn('WARNING:',output.getvalue())

    def test_cancel_never_deletes(self):
        with patch.object(z,'run',side_effect=self.read) as run,patch('builtins.input',return_value='yes'):
            with self.assertRaises(z.Error):z.purge_snapshots(self.groups(),'1-3')
            self.assertFalse(any(c.args[1] in ('destroy','release') for c in run.call_args_list))

    def test_guid_change_aborts(self):
        with patch.object(z,'run',return_value='wrong'):
            with self.assertRaises(z.Error):z.inspect_purge(self.groups()[:1])

    def test_clone_dependency_aborts(self):
        with patch.object(z,'run',side_effect=['123','rpool/clone']):
            with self.assertRaisesRegex(z.Error,'clones'):z.inspect_purge(self.groups()[:1])

    def test_foreign_hold_aborts_even_on_backup(self):
        def read(*args,**kwargs):
            if args[1]=='holds':return args[-1]+'\tother-tool\tdate'
            return self.read(*args,**kwargs)
        with patch.object(z,'run',side_effect=read):
            with self.assertRaisesRegex(z.Error,'another hold'):z.inspect_purge(self.groups()[:1],backup=True)

    def test_backup_hold_released_and_metadata_retired_before_destroy(self):
        with tempfile.TemporaryDirectory() as tmp:
            point=Path(tmp);(point/'SHA256SUMS').write_text('checksums')
            def read(*args,**kwargs):
                if args[1]=='holds':return args[-1]+'\tlinux_os_backup\tdate'
                if args[1]=='destroy':self.assertFalse((point/'SHA256SUMS').exists())
                return self.read(*args,**kwargs)
            with patch.object(z,'run',side_effect=read) as run, \
                 patch.object(z,'retire_recovery_points',return_value=[point]), \
                 patch('builtins.input',return_value='PURGE 1-1'):
                z.purge_snapshots(self.groups(),'1-1',backup=True,hostdir=point,namespace='rpool')
                releases=[c.args for c in run.call_args_list if c.args[1]=='release']
                self.assertEqual(len(releases),2)
                self.assertTrue((point/'SHA256SUMS.purged').exists())

    def test_purging_backup_tip_invalidates_generation_but_old_points_do_not(self):
        groups=self.groups()
        for group in groups:
            group['snapshots']=[('store/ns/generation/'+name,guid) for name,guid in group['snapshots']]
        for selection,expected in [('1-3',False),('5-5',True)]:
            with patch.object(z,'inspect_purge'),patch.object(z,'retire_recovery_points',return_value=[]), \
                 patch.object(z,'run',side_effect=lambda *a,**k:'-g' if a[:2]==('zpool','reguid') else ('zfs-kmod-2.3.0' if a==('zfs','version') else '')) as run,patch('builtins.input',return_value='PURGE '+selection):
                z.purge_snapshots(groups,selection,backup=True,namespace='store/ns')
                marked=[c.args for c in run.call_args_list if c.args[:2]==('zfs','set')]
                self.assertEqual(bool(marked),expected)
                if expected:self.assertEqual(marked[0][-1],'store/ns/generation')

    def test_changed_snapshot_after_confirmation_prevents_mutation(self):
        with patch.object(z,'inspect_purge',side_effect=[None,z.Error('snapshot changed')]), \
             patch('builtins.input',return_value='PURGE 1-1'),patch.object(z,'run') as run:
            with self.assertRaises(z.Error):z.purge_snapshots(self.groups(),'1-1')
            run.assert_not_called()

    def test_failed_destroy_restores_backup_hold(self):
        def read(*args,**kwargs):
            if args[1]=='holds':return args[-1]+'\tlinux_os_backup\tdate'
            if args[1]=='destroy':raise z.Error('I/O error')
            return self.read(*args,**kwargs)
        with patch.object(z,'run',side_effect=read) as run,patch.object(z,'retire_recovery_points',return_value=[]), \
             patch('builtins.input',return_value='PURGE 1-1'):
            with self.assertRaises(z.Error):z.purge_snapshots(self.groups(),'1-1',backup=True)
            self.assertTrue(any(c.args[:3]==('zfs','hold','linux_os_backup') for c in run.call_args_list))


class BackupDiscoveryTests(unittest.TestCase):
    def setUp(self):
        signatures=patch.object(z,'signatures',return_value=[])
        signatures.start();self.addCleanup(signatures.stop)

    def disks(self):
        return [{'type':'disk','path':'/dev/'+name,'size':100*z.GIB,'children':[
            {'fstype':'zfs_member','label':label}]} for name,label in
            [('a','rpool'),('b','linux_os_backup_one'),('c','baremetal_store_two')]]+[
            {'type':'disk','path':'/dev/blank','size':100*z.GIB}]

    def test_lists_existing_and_standalone_disks_and_selects_existing(self):
        with patch.object(z,'inventory',return_value={'blockdevices':self.disks()}), \
             patch.object(z,'target_idle'),patch('builtins.input',return_value='2'),patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(z.choose_backup_storage(),'/dev/c')
            self.assertIn('1. /dev/b',output.getvalue());self.assertIn('2. /dev/c',output.getvalue())
            self.assertIn('/dev/a ',output.getvalue());self.assertIn('/dev/blank',output.getvalue())
            self.assertIn('Available backup targets:',output.getvalue())
            self.assertIn('Existing backup disk;',output.getvalue())
            self.assertIn('Other disk; contains data',output.getvalue())
            self.assertIn('Other disk; empty',output.getvalue())

    def test_single_disk_automatically_selected_without_prompt(self):
        with patch.object(z,'inventory',return_value={'blockdevices':[self.disks()[1]]}), \
             patch('builtins.input') as prompt:
            self.assertEqual(z.choose_backup_storage(),'/dev/b')
            prompt.assert_not_called()

    def test_restore_single_backup_alongside_other_disks_skips_selection(self):
        disks=self.disks()[:2]+[self.disks()[-1]]
        with patch.object(z,'inventory',return_value={'blockdevices':disks}), \
             patch.object(z,'target_idle'),patch('builtins.input') as prompt, \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(z.choose_backup_storage(for_restore=True),'/dev/b')
        prompt.assert_not_called()
        self.assertIn('Available backup sources:',output.getvalue())
        self.assertNotIn('Available backup targets:',output.getvalue())
        self.assertIn('Existing backup disk; linux_os_backup_one (default)',output.getvalue())
        self.assertIn('Automatically selected backup source: /dev/b',output.getvalue())
        self.assertIn('/dev/a',output.getvalue())
        self.assertIn('Other disk; contains data',output.getvalue())
        self.assertIn('Other disk; empty',output.getvalue())

    def test_enter_accepts_preselected_backup_after_prompt(self):
        with patch.object(z,'inventory',return_value={'blockdevices':self.disks()[:2]}), \
             patch.object(z,'target_idle'),patch('builtins.input',return_value='') as prompt, \
             patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(z.choose_backup_storage(),'/dev/b')
        prompt.assert_called_once()
        self.assertIn('[1]',prompt.call_args.args[0])

    def test_multiple_existing_backups_have_no_arbitrary_default(self):
        with patch.object(z,'inventory',return_value={'blockdevices':self.disks()[1:3]}), \
             patch('builtins.input',return_value='') as prompt,patch('sys.stdout',new_callable=io.StringIO) as output, \
             self.assertRaisesRegex(z.Error,'No backup storage selected'):
            z.choose_backup_storage(for_restore=True)
        prompt.assert_called_once()
        self.assertEqual(prompt.call_args.args[0],'Backup source disk number or device: ')
        self.assertNotIn('[',prompt.call_args.args[0])
        self.assertNotIn('(default)',output.getvalue())

    def test_standalone_disk_cannot_be_selected_for_reading_backups(self):
        with patch.object(z,'inventory',return_value={'blockdevices':self.disks()}), \
             patch.object(z,'target_idle'),patch('builtins.input',return_value='3'), \
             patch('sys.stdout',new_callable=io.StringIO),self.assertRaisesRegex(z.Error,'no backups to read'):
            z.choose_backup_storage()

    def test_in_use_nonbackup_disks_are_hidden(self):
        with patch.object(z,'inventory',return_value={'blockdevices':self.disks()[:2]}), \
             patch.object(z,'target_idle',side_effect=z.Error('disk in use')), \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(z.choose_backup_storage(),'/dev/b')
        self.assertNotIn('/dev/a',output.getvalue())
        self.assertNotIn('Unavailable destinations',output.getvalue())

    def test_single_management_host_automatically_selected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'hosts'/'proxmox2').mkdir(parents=True)
            def read(*args,**kwargs):
                if args[0]=='findmnt':return json.dumps({'filesystems':[{'target':tmp}]})
                return 'on'
            with patch.object(z,'zfs_repository',return_value=('store','123')), \
                 patch.object(z,'run',side_effect=read),patch('builtins.input') as prompt:
                repository,hostdir,namespace=z.management_host(root)
                self.assertEqual(hostdir,root/'hosts'/'proxmox2')
                self.assertTrue(namespace.endswith('/proxmox2'))
                prompt.assert_not_called()

    def test_missing_backup_disk_refreshes_until_attached(self):
        unrelated=[self.disks()[0],self.disks()[-1]]
        scans=[{'blockdevices':unrelated},{'blockdevices':[]},
               {'blockdevices':unrelated+[self.disks()[1]]}]
        with patch.object(z,'inventory',side_effect=scans) as inventory, \
             patch.object(z,'target_idle'),patch('builtins.input',side_effect=['','','1']) as prompt, \
             patch.object(z,'run') as run,patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(z.choose_backup_storage(),'/dev/b')
        self.assertEqual(inventory.call_count,3)
        self.assertEqual(prompt.call_count,3)
        self.assertIn('Attach a backup disk',output.getvalue())
        self.assertTrue(all('Enter to refresh, or q to quit' in c.args[0] for c in prompt.call_args_list[:2]))
        run.assert_not_called()

    def test_refresh_with_multiple_disks_still_requires_selection(self):
        with patch.object(z,'inventory',side_effect=[{'blockdevices':[]},{'blockdevices':self.disks()}]), \
             patch.object(z,'target_idle'),patch('builtins.input',side_effect=['','2']),patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(z.choose_backup_storage(),'/dev/c')

    def test_missing_backup_disk_quit_exits_cleanly_after_invalid_input(self):
        with patch.object(z.os,'geteuid',return_value=0), \
             patch.object(z,'verify',side_effect=lambda args:z.choose_backup_storage()), \
             patch.object(z,'inventory',return_value={'blockdevices':[]}) as inventory, \
             patch('builtins.input',side_effect=['bad','',' Q ']) as prompt, \
             patch.object(z,'run') as run,patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(z.main(['verify']),0)
        self.assertEqual(inventory.call_count,2)
        self.assertEqual(prompt.call_count,3)
        self.assertIn('Backup disk selection cancelled.',output.getvalue())
        run.assert_not_called()

    def test_invalid_number_rejected(self):
        with patch.object(z,'inventory',return_value={'blockdevices':self.disks()}), \
             patch.object(z,'target_idle'),patch('builtins.input',return_value='5'):
            with self.assertRaises(z.Error):z.choose_backup_storage()


class UnifiedCommandTests(unittest.TestCase):
    def test_backup_dispatch(self):
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'backup') as backup:
            self.assertEqual(z.main(['backup','--destination','/dev/test','--full']),0)
            args=backup.call_args.args[0]
            self.assertEqual(args.destination,'/dev/test');self.assertTrue(args.full)

    def test_restore_dispatch(self):
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'restore') as restore:
            self.assertEqual(z.main(['restore','--source','/repo','--dry-run'],restore_isolated=True),0)
            self.assertTrue(restore.call_args.args[0].dry_run)

    def test_snapshot_dispatch(self):
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'snapshots_main',return_value=0) as snapshots:
            self.assertEqual(z.main(['snapshots','--backup','--host','proxmox2']),0)
            args=snapshots.call_args.args[0]
            self.assertEqual(args.backup,'');self.assertEqual(args.host,'proxmox2')

    def test_no_arguments_show_usage_without_root_or_prompts(self):
        with patch.object(z.os,'geteuid',return_value=1000),patch('builtins.input') as prompt, \
             patch('sys.stdout',new_callable=io.StringIO) as output, \
             patch.object(z,'backup') as backup,patch.object(z,'restore') as restore:
            self.assertEqual(z.main([]),0)
            self.assertIn('lllzorb — usage',output.getvalue())
            self.assertIn('SNAPSHOT MANAGEMENT',output.getvalue())
            prompt.assert_not_called();backup.assert_not_called();restore.assert_not_called()

    def test_copied_single_file_runs_without_companion_files(self):
        source=Path(__file__).resolve().parents[1]/'lllzorb'
        with tempfile.TemporaryDirectory() as tmp:
            standalone=Path(tmp)/'lllzorb';z.shutil.copy2(source,standalone)
            result=z.subprocess.run([str(standalone),'snapshots','--help'],cwd=tmp,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn('--purge',result.stdout)
            self.assertEqual([p.name for p in Path(tmp).iterdir()],['lllzorb'])


class CloneCommandTests(unittest.TestCase):
    """Tests for the `clone` command (live boot disk -> bootable spare disk).

    Mirrors the CLONE_PLAN.md test plan. The same-host materialization and the
    ephemeral-snapshot lifecycle are already exercised by CloneRestoreTests and
    EphemeralBackupTests; here we cover the new command-specific behavior.
    """

    def test_clone_command_dispatch(self):
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'clone') as clone:
            self.assertEqual(z.main(['clone','--target','/dev/test','--dry-run'],restore_isolated=True),0)
            args=clone.call_args.args[0]
            self.assertEqual(args.target,'/dev/test');self.assertTrue(args.dry_run)

    def test_clone_flag_parsing(self):
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'clone') as clone:
            self.assertEqual(z.main(['clone','--target','/dev/t','--swap','4096','--discard','on',
                                     '--allow-small-target','--snapshot_name','snap1','--keep-snapshot',
                                     '--host','host1','--unattended','--confirm','--dry-run'],
                                    restore_isolated=True),0)
            a=clone.call_args.args[0]
            self.assertEqual(a.target,'/dev/t');self.assertEqual(a.swap,4096)
            self.assertEqual(a.discard,'on');self.assertTrue(a.allow_small_target)
            self.assertEqual(a.snapshot_name,'snap1');self.assertTrue(a.keep_snapshot)
            self.assertEqual(a.host,'host1');self.assertTrue(a.unattended)
            self.assertTrue(a.confirm);self.assertTrue(a.dry_run)

    def test_clone_unattended_requires_target_and_confirm(self):
        args=z.argparse.Namespace(unattended=True,confirm=False,dry_run=False,target=None)
        with patch.object(z,'commands'),patch.object(z,'discover') as discover:
            with self.assertRaisesRegex(z.Error,'requires --target and --confirm'):
                z.clone(args)
            discover.assert_not_called()

    def test_native_dataset_streams_from_live_pool(self):
        # The clone trick: point native_dataset at the live pool so the send is
        # `zfs send -R -b <pool>@<snap>` straight from the running system.
        m=native_fixture();pool=m['pools'][0]
        pool['native_dataset']=pool['name']
        pool.pop('datasets',None);pool.pop('stream',None);pool['encrypted']=False
        snap=m['snapshot']
        expected=['zfs','send','-R','-b',f"{pool['name']}@{snap}"]
        self.assertEqual(z.native_send_argv(pool,snap),expected)
        self.assertEqual(z.full_restore_streams(pool,snap,stack=True),[('',expected)])

    def test_wait_partition_nodes_waits_for_lagging_kernel_node(self):
        layout=[dict(number=1),dict(number=2)]
        calls={'n':0}
        def node_for(path,*a,**k):
            if 'part1' in str(path):
                calls['n']+=1
                if calls['n']<3:
                    raise z.Error('Block device not found: '+str(path))
            return {'type':'part'}
        with patch.object(z,'stable_device',side_effect=lambda p:p), patch.object(z,'node_for',side_effect=node_for), patch.object(z.time,'sleep'):
            z.wait_partition_nodes('/dev/disk/by-id/wwn-D',layout)
        self.assertGreaterEqual(calls['n'],3)

    def test_wait_partition_nodes_errors_when_rescan_never_finishes(self):
        layout=[dict(number=3)]
        with patch.object(z,'stable_device',side_effect=lambda p:p), patch.object(z,'node_for',side_effect=z.Error('Block device not found: x')), patch.object(z.time,'sleep'), patch.object(z,'PARTITION_RESYNC_TIMEOUT',0):
            with self.assertRaisesRegex(z.Error,'did not appear'):
                z.wait_partition_nodes('/dev/disk/by-id/wwn-D',layout)

    def test_source_disk_rejected_as_clone_target(self):
        # The running source disk is passed as a protected disk to target_idle,
        # which rejects it with a 'protected disk' error.
        device='/dev/disk/by-id/wwn-SOURCE'
        node={'type':'disk','path':device,'ro':False}
        fake_stat=unittest.mock.MagicMock(st_mode=z.stat.S_IFBLK)
        with patch.object(z,'node_for',return_value=node), \
             patch.object(z,'stable_device',side_effect=lambda d,a=None:d), \
             patch.object(z.os,'stat',return_value=fake_stat), \
             patch.object(z.os.path,'realpath',side_effect=lambda p:p):
            with self.assertRaisesRegex(z.Error,'protected disk'):
                z.target_idle(device,forbidden={device})




class BackupCloneTests(unittest.TestCase):
    def test_new_layout_uses_whole_target_for_data(self):
        for sector in (512,4096):
            p=z.clone_layout(100*z.GIB,sector,20*z.GIB)[0]
            self.assertEqual(p['start_lba']*sector,z.MIB)
            self.assertEqual((p['end_lba']+1)*sector,100*z.GIB-z.MIB)
            self.assertEqual(p['type_guid'],z.ZFS)

    def test_smaller_target_allowed_when_contents_fit(self):
        self.assertEqual(len(z.clone_layout(50*z.GIB,512,10*z.GIB)),1)
        with self.assertRaises(z.Error):z.clone_layout(5*z.GIB,512,10*z.GIB)

    def test_rebase_changes_storage_identity_only(self):
        m=native_fixture();original=copy.deepcopy(m)
        new=z.rebase_manifest(m,'store','linux_os_backup_clone','777')
        self.assertEqual(m,original)
        self.assertEqual(new['disk'],m['disk'])
        self.assertEqual(new['pools'][0]['guid'],m['pools'][0]['guid'])
        self.assertEqual(new['pools'][0]['datasets'],m['pools'][0]['datasets'])
        self.assertEqual(new['storage_pool_guid'],'777')
        self.assertTrue(new['native_root'].startswith('linux_os_backup_clone/'))
        z.validate(new)

    def test_rebase_rejects_external_native_data(self):
        m=native_fixture();m['pools'][0]['native_dataset']='outside/data'
        with self.assertRaises(z.Error):z.rebase_manifest(m,'store','clone','777')

    def test_metadata_rewrite_preserves_archive_checksum_and_verifies_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            point=Path(tmp)/'hosts/proxmox2/backup-20260914-183000';point.mkdir(parents=True)
            manifest=point/'manifest.json';manifest.write_text(json.dumps(native_fixture()))
            (point/'archive').write_bytes(b'archive bytes')
            archive_hash=z.digest(point/'archive')
            (point/'SHA256SUMS').write_text(z.digest(manifest)+'  manifest.json\n'+archive_hash+'  archive\n')
            self.assertEqual(z.rebase_clone_metadata(tmp,'store','clone','777'),[point])
            self.assertIn(z.digest(manifest)+'  manifest.json',(point/'SHA256SUMS').read_text())
            self.assertIn(archive_hash+'  archive',(point/'SHA256SUMS').read_text())
            self.assertEqual(json.loads(manifest.read_text())['storage_pool_guid'],'777')

    def test_snapshot_guid_map_ignores_only_temporary_clone_snapshot(self):
        rows='old/a@keep\t123\nold/a@backup-copy-test\t999\nold@older\t456\n'
        with patch.object(z,'run',return_value=rows):
            self.assertEqual(z.snapshot_guid_map('old','backup-copy-test'),{'/a@keep':'123','@older':'456'})

    def exercise_clone_boundary(self,target_size,dry_run):
        import argparse
        with tempfile.TemporaryDirectory() as tmp:
            args=argparse.Namespace(source='/dev/source',target='/dev/target',dry_run=dry_run)
            source={'type':'disk','size':200*z.GIB}
            target={'type':'disk','size':target_size,'log-sec':512,'phy-sec':4096}
            pp={'health':{'value':'ONLINE'},'allocated':{'value':str(10*z.GIB)}}
            dp={'used':{'value':str(10*z.GIB)},'logicalused':{'value':str(10*z.GIB)}}
            def read(*args,**kwargs):
                if args[:3]==('zfs','send','-n'):return 'size\t'+str(10*z.GIB)+'\n'
                return ''
            with patch.object(z,'stable_device',side_effect=str), patch.object(z,'commands'),patch.object(z,'node_for',return_value=source), \
                 patch.object(z,'target_idle',return_value=target), \
                 patch.object(z,'management_storage',return_value=z.contextlib.nullcontext(Path(tmp))), \
                 patch.object(z,'zfs_repository',return_value=('old','123')), \
                 patch.object(z,'props',side_effect=[{'old':pp},{'old':dp}]), \
                 patch.object(z,'run',side_effect=read) as run, \
                 patch.object(z,'confirm',side_effect=z.Error('declined')) as confirm, \
                 patch.object(z,'create_layout') as create:
                try:
                    z.clone_backup(args)
                finally:
                    create.assert_not_called()
                    if dry_run or target_size<10*z.GIB:
                        confirm.assert_not_called()
                        self.assertFalse(any(c.args[:2] in [('zfs','snapshot'),('zfs','destroy')] for c in run.call_args_list))
                    else:
                        confirm.assert_called_once_with('/dev/target')
                        destroyed=[c.args for c in run.call_args_list if c.args[:2]==('zfs','destroy')]
                        self.assertEqual(len(destroyed),1)
                        self.assertTrue(destroyed[0][-1].startswith('old@backup-copy-'))

    def test_clone_dry_run_never_snapshots_or_writes_target(self):
        self.exercise_clone_boundary(100*z.GIB,True)

    def test_clone_capacity_failure_never_snapshots_or_writes_target(self):
        with self.assertRaises(z.Error):self.exercise_clone_boundary(5*z.GIB,False)

    def test_clone_declined_confirmation_only_cleans_temporary_source_snapshot(self):
        with self.assertRaisesRegex(z.Error,'declined'):self.exercise_clone_boundary(100*z.GIB,False)

    def test_clone_command_dispatch(self):
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'clone_backup') as clone:
            self.assertEqual(z.main(['clone-backup','--source','/dev/a','--target','/dev/b','--dry-run']),0)
            args=clone.call_args.args[0]
            self.assertTrue(args.dry_run);self.assertEqual(args.source,'/dev/a');self.assertEqual(args.target,'/dev/b')


if __name__=='__main__':unittest.main()

class RestoreStorageSelectionTests(unittest.TestCase):
    def test_discovery_keeps_storage_open_and_cleans_up_on_failure(self):
        from argparse import Namespace
        events=[]
        @z.contextlib.contextmanager
        def storage(path):
            self.assertEqual(path,'/dev/backup')
            events.append('open')
            try: yield Path('/mounted')
            finally: events.append('close')
        def recover(args,base):
            self.assertEqual(base,Path('/mounted'))
            self.assertEqual(events,['open'])
            raise z.Error('validation failed')
        with patch.object(z,'commands'),patch.object(z,'repository_reader',side_effect=lambda base,*a:z.contextlib.nullcontext(base)), patch.object(z,'choose_backup_storage',return_value='/dev/backup') as choose, \
             patch.object(z,'management_storage',side_effect=storage), patch.object(z,'restore_from_storage',side_effect=recover):
            with self.assertRaises(z.Error):z.restore(Namespace(backup=None))
            choose.assert_called_once_with(for_restore=True)
        self.assertEqual(events,['open','close'])

    def test_explicit_storage_skips_discovery(self):
        from argparse import Namespace
        with patch.object(z,'commands'),patch.object(z,'repository_reader',side_effect=lambda base,*a:z.contextlib.nullcontext(base)), patch.object(z,'choose_backup_storage') as choose, \
             patch.object(z,'management_storage',return_value=z.contextlib.nullcontext(Path('/mounted'))) as storage, \
             patch.object(z,'restore_from_storage') as recover:
            args=Namespace(backup='/dev/backup')
            z.restore(args)
            choose.assert_not_called()
            storage.assert_called_once_with('/dev/backup')
            recover.assert_called_once_with(args,Path('/mounted'))

    def test_single_restore_host_needs_no_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            host=Path(tmp)/'hosts'/'nas';host.mkdir(parents=True)
            with patch.object(z,'catalog',return_value=[(host/'backup-example',{'hostname':'nas'})]), \
                 patch('builtins.input',side_effect=AssertionError('unexpected prompt')):
                self.assertEqual(z.select_host_repository(tmp),host)


class VerifyCommandTests(unittest.TestCase):
    def test_full_verification_of_selected_snapshot(self):
        from argparse import Namespace
        args=Namespace(backup=None,host='nas',snapshot='selected',list_snapshots=False)
        with patch.object(z,'commands'),patch.object(z,'repository_reader',side_effect=lambda base,*a:z.contextlib.nullcontext(base)), patch.object(z,'choose_backup_storage',return_value='/dev/backup'), \
             patch.object(z,'management_storage',return_value=z.contextlib.nullcontext(Path('/repo'))), \
             patch.object(z,'select_backup',return_value=Path('/repo/point')) as select, \
             patch.object(z,'verify_chain',return_value=[(Path('/repo/point'),{'snapshot':'selected'})]) as check:
            z.verify(args)
            select.assert_called_once_with(Path('/repo'),'selected',False,'nas')
            check.assert_called_once_with(Path('/repo/point'))

    def test_cli_dispatch(self):
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'verify') as verify:
            self.assertEqual(z.main(['verify','--backup','/dev/backup','--host','nas']),0)
            self.assertEqual(verify.call_args.args[0].backup,'/dev/backup')


class CommandOutputEncodingTests(unittest.TestCase):
    def test_non_utf8_output_is_lossless(self):
        payload=b'ONLINE\nfile-\xfe\n'
        result=z.subprocess.CompletedProcess(['zpool'],0,payload,b'')
        with patch.object(z.subprocess,'run',return_value=result):
            output=z.run('zpool','status','-LP')
        self.assertEqual(output.encode('utf-8','surrogateescape'),payload)

    def test_combined_output_preserves_non_utf8_stderr(self):
        result=z.subprocess.CompletedProcess(['tool'],0,b'ok\n',b'label-\xfe')
        with patch.object(z.subprocess,'run',return_value=result):
            output=z.run('tool',combined=True)
        self.assertEqual(output.encode('utf-8','surrogateescape'),b'ok\nlabel-\xfe')

class GuidHandoffTests(unittest.TestCase):
    def handoff(self,fail_export=False):
        pools={'running':'999','restore_temp':'456'};calls=[]
        def run(*args,**kwargs):
            calls.append(args)
            if args[:2]==('zpool','get'):return pools[args[-1]]
            if args[:2]==('zpool','list'):
                return '\n'.join(name+'\t'+guid for name,guid in pools.items())
            if args[:2]==('zpool','reguid'):
                name=args[-1];new=args[3] if '-g' in args else '789'
                if new in pools.values():raise z.Error('GUID collision')
                pools[name]=new;return ''
            if args[:2]==('zpool','export'):
                if fail_export:raise z.Error('busy')
                del pools[args[-1]];return ''
            raise AssertionError(args)
        with tempfile.TemporaryDirectory() as tmp,patch.object(z,'run',side_effect=run):
            journal=Path(tmp)/'journal.json'
            if fail_export:
                with self.assertRaises(z.Error):z.transfer_pool_guid('restore_temp','123',None,journal)
                self.assertEqual(pools['restore_temp'],'123')
            else:
                z.transfer_pool_guid('restore_temp','123',None,journal)
                self.assertNotIn('restore_temp',pools)
                self.assertEqual(json.loads(journal.read_text())['phase'],'complete')
            self.assertEqual(pools['running'],'999')
        self.assertFalse(any(c[:2] in (('zpool','export'),('zpool','reguid')) and c[-1]=='running' for c in calls))

    def test_success_changes_only_destination(self):self.handoff()
    def test_export_failure_never_changes_running_pool(self):self.handoff(True)
    def test_known_conflict_fails_without_commands_or_confirmation(self):
        conflict={'name':'rpool','guid':'123'}
        with patch.object(z,'run') as run,patch('builtins.input') as prompt:
            with self.assertRaisesRegex(z.Error,'recovery environment'):
                z.transfer_pool_guid('restore_temp','123',conflict,Path('/unused'))
            run.assert_not_called();prompt.assert_not_called()

    def test_late_conflict_fails_without_pool_changes(self):
        with patch.object(z,'run',return_value='running\t123\nrestore_temp\t456') as run:
            with self.assertRaisesRegex(z.Error,'now in use'):
                z.transfer_pool_guid('restore_temp','123',None,Path('/unused'))
            self.assertEqual(run.call_args_list,[unittest.mock.call('zpool','list','-H','-o','name,guid')])

    def test_full_restore_accepts_imported_source_identity_and_still_confirms_target(self):
        m=native_fixture()
        args=z.argparse.Namespace(target='/dev/target',dry_run=False,discard='off')
        disk={'size':500*z.GIB,'log-sec':512,'phy-sec':512}
        conflicts=[{'name':'running','guid':'123'}]
        with patch.object(z,'select_backup',return_value=Path('/repo/point')), \
             patch.object(z,'verify_chain',return_value=[(Path('/repo/point'),m)]), \
             patch.object(z,'commands'),patch.object(z,'run',return_value='') as run, \
             patch.object(z,'estimate_native_send',return_value=1000), \
             patch.object(z,'protected_path',return_value=set()),patch.object(z,'stable_device',side_effect=str), \
             patch.object(z,'target_idle',return_value=disk),patch.object(z,'print_existing_layout'), \
             patch.object(z,'guid_conflicts',return_value=conflicts), \
             patch.object(z,'clone_restore') as restore,patch.object(z,'confirm') as confirm:
            z.restore_from_storage(args,Path('/repo'))
            confirm.assert_called_once_with('/dev/target')
            self.assertEqual(restore.call_args.args[4],conflicts)
            self.assertFalse(any(c.args[:2] in [('zpool','reguid'),('zpool','export')] for c in run.call_args_list))
    def test_conflict_matches_guid_not_name(self):
        with patch.object(z,'run',return_value='othername\t123\nrpool\t999'):
            self.assertEqual(z.guid_conflicts({'pools':[{'name':'rpool','guid':'123'}]}),[{'name':'othername','guid':'123'}])

class InitramfsRepairTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)

    def file(self,name,data=b'x',executable=False):
        path=self.root/name.lstrip('/')
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
        if executable:path.chmod(0o755)
        return path

    def installation(self,dracut=True):
        for name in (('dracut','lsinitrd') if dracut else ('update-initramfs','lsinitramfs')):
            self.file('/usr/bin/'+name,executable=True)
        for kernel in ('6.8-test','7.0-test'):
            self.file('/boot/vmlinuz-'+kernel)
            (self.root/'usr/lib/modules'/kernel).mkdir(parents=True,exist_ok=True)

    def generate(self,*args):
        if args[2].endswith('/dracut'):
            self.file(args[-2],b'new initrd')
        elif args[2].endswith('/update-initramfs'):
            self.file('/boot/initrd.img-'+args[-1],b'new initrd')
        return ''

    def test_dracut_wins_over_retained_initramfs_tools(self):
        self.installation()
        self.file('/usr/bin/update-initramfs',executable=True)
        with patch.object(z,'run',side_effect=self.generate) as run:
            self.assertEqual(z.rebuild_initramfs(self.root),'dracut')
        for kernel in ('6.8-test','7.0-test'):
            self.assertIn(('chroot',self.root,'/usr/bin/dracut','--force',
                           '--no-hostonly','--no-hostonly-cmdline',
                           '/boot/initrd.img-'+kernel,kernel),[c.args for c in run.call_args_list])
            self.assertIn(('chroot',self.root,'/usr/bin/lsinitrd','/boot/initrd.img-'+kernel),
                          [c.args for c in run.call_args_list])
        self.assertFalse(any(c.args[2].endswith('update-initramfs') for c in run.call_args_list))

    def test_initramfs_tools_updates_existing_and_creates_missing(self):
        self.installation(dracut=False)
        self.file('/boot/initrd.img-6.8-test')
        with patch.object(z,'run',side_effect=self.generate) as run:
            self.assertEqual(z.rebuild_initramfs(self.root),'initramfs-tools')
        for mode,kernel in (('-u','6.8-test'),('-c','7.0-test')):
            self.assertIn(('chroot',self.root,'/usr/bin/update-initramfs',mode,'-k',kernel),
                          [c.args for c in run.call_args_list])

    def test_missing_generator_fails_without_commands(self):
        with patch.object(z,'run') as run,self.assertRaisesRegex(z.Error,'neither dracut'):
            z.rebuild_initramfs(self.root)
        run.assert_not_called()

    def test_missing_modules_fails_before_rebuilding_any_kernel(self):
        self.installation()
        (self.root/'usr/lib/modules/7.0-test').rmdir()
        with patch.object(z,'run') as run,self.assertRaisesRegex(z.Error,'Missing kernel modules'):
            z.rebuild_initramfs(self.root)
        run.assert_not_called()

    def test_missing_kernels_and_inspector_fail_before_rebuild(self):
        self.installation()
        for path in (self.root/'boot').glob('vmlinuz-*'):path.unlink()
        with patch.object(z,'run') as run,self.assertRaisesRegex(z.Error,'No installed kernels'):
            z.rebuild_initramfs(self.root)
        run.assert_not_called()
        (self.root/'usr/bin/lsinitrd').unlink()
        with patch.object(z,'run') as run,self.assertRaisesRegex(z.Error,'inspection utility'):
            z.rebuild_initramfs(self.root)
        run.assert_not_called()

    def test_empty_or_missing_output_fails(self):
        for empty in (False,True):
            with self.subTest(empty=empty):
                self.installation()
                if empty:self.file('/boot/initrd.img-6.8-test',b'')
                with patch.object(z,'run',return_value=''),self.assertRaisesRegex(z.Error,'Missing or empty'):
                    z.rebuild_initramfs(self.root)

    def test_generation_and_inspection_errors_propagate(self):
        self.installation()
        for fail_inspection in (False,True):
            def execute(*args):
                if not fail_inspection or args[2].endswith('/lsinitrd'):
                    raise z.Error('initrd failure')
                return self.generate(*args)
            with self.subTest(inspection=fail_inspection),patch.object(z,'run',side_effect=execute), \
                 self.assertRaisesRegex(z.Error,'initrd failure'):
                z.rebuild_initramfs(self.root)

    def test_backup_has_no_boot_generator_dependency(self):
        with patch.object(z,'commands') as commands,patch.object(z,'discover',side_effect=z.Error('stop')):
            with self.assertRaisesRegex(z.Error,'stop'):z.backup(None)
        required=commands.call_args.args[0].split()
        for command in ('dracut','update-initramfs','update-grub','grub-install'):
            self.assertNotIn(command,required)


class ActiveDracutRestoreTests(unittest.TestCase):
    setUp=InitramfsRepairTests.setUp
    file=InitramfsRepairTests.file
    installation=InitramfsRepairTests.installation
    listing='usr/lib/modules/7.0-test/zfs.ko.zst\nusr/sbin/zpool\nusr/lib/systemd/system/zfs-import-scan.service\n'

    def generate(self,*args):
        if args[2].endswith('/dracut'):
            self.assertFalse((self.root/'etc/zfs/zpool.cache').exists())
            self.file(args[-2],('image for '+args[-1]).encode())
            return ''
        return self.listing

    def test_rebuild_replaces_images_and_removes_old_cache(self):
        self.installation()
        self.file('/etc/zfs/zpool.cache',b'old vdev GUIDs')
        self.file('/boot/initrd.img-6.8-test',b'old initrd')
        with patch.object(z,'run',side_effect=self.generate) as run,patch.object(z.os,'sync'):
            self.assertEqual(z.rebuild_restored_dracut(self.root),['6.8-test','7.0-test'])
        self.assertEqual((self.root/'boot/initrd.img-6.8-test').read_bytes(),b'image for 6.8-test')
        self.assertFalse((self.root/'etc/zfs/zpool.cache').exists())
        builds=[c.args for c in run.call_args_list if c.args[2].endswith('/dracut')]
        for build in builds:
            self.assertIn('--no-hostonly',build)
            self.assertIn('--no-hostonly-cmdline',build)
            self.assertIn('--no-uefi',build)
            self.assertEqual(build[build.index('--add')+1],'zfs')
        self.assertEqual(len(builds),2)

    def test_no_dracut_preserves_images_and_cache(self):
        self.installation(dracut=False)
        cache=self.file('/etc/zfs/zpool.cache',b'original')
        with patch.object(z,'run') as run:
            self.assertEqual(z.rebuild_restored_dracut(self.root),[])
        run.assert_not_called()
        self.assertEqual(cache.read_bytes(),b'original')

    def test_bad_image_restores_cache_and_keeps_original_initrds(self):
        self.installation()
        cache=self.file('/etc/zfs/zpool.cache',b'original')
        original=self.file('/boot/initrd.img-6.8-test',b'original initrd')
        for listing in ('',self.listing+'etc/zfs/zpool.cache\n',
                        self.listing.replace('zfs.ko.zst','other.ko'),
                        self.listing.replace('usr/sbin/zpool','usr/sbin/other'),
                        self.listing.replace('zfs-import-scan.service','other.service')):
            self.listing=listing
            with self.subTest(listing=listing),patch.object(z,'run',side_effect=self.generate),self.assertRaises(z.Error):
                z.rebuild_restored_dracut(self.root)
            self.assertEqual(cache.read_bytes(),b'original')
            self.assertEqual(original.read_bytes(),b'original initrd')
            self.assertFalse(list((self.root/'boot').glob('.restore-initrd-*')))

    def test_second_kernel_failure_does_not_replace_first(self):
        self.installation()
        original=self.file('/boot/initrd.img-6.8-test',b'original initrd')
        def execute(*args):
            if args[-1]=='7.0-test':raise z.Error('second kernel failed')
            return self.generate(*args)
        with patch.object(z,'run',side_effect=execute),self.assertRaisesRegex(z.Error,'second kernel'):
            z.rebuild_restored_dracut(self.root)
        self.assertEqual(original.read_bytes(),b'original initrd')

    def test_namespace_payload_uses_only_destination_aliases_and_partitions(self):
        m=ubuntu_fixture();m['boot']['manager']='proxmox-grub'
        m['mounts'].append(dict(source='rpool/USERDATA/test',target='/home/test'))
        restored=[(p,'restore_'+p['name']) for p in m['pools']]
        with patch.object(z,'restored_fstab_replacements',return_value={}),patch.object(z,'run',return_value='') as run, \
             patch.object(z,'partition_device',side_effect=lambda d,n:d+'-p'+str(n)):
            z.refresh_restored_dracut(m,restored,'/dev/target',m['disk']['partitions'])
        args=run.call_args.args
        self.assertEqual(args[:4],('unshare','--mount','--propagation','private'))
        self.assertEqual(args[4:7],(sys.executable,'-u','-c'))
        self.assertTrue(run.call_args.kwargs['live'])
        payload=json.loads(run.call_args.kwargs['input'])
        self.assertEqual(payload['mounts'],[dict(source='restore_rpool/ROOT/ubuntu_1opcom',target='/'),
                                         dict(source='restore_bpool/BOOT/ubuntu_1opcom',target='/boot')])
        self.assertEqual(payload['esps'],['/dev/target-p1'])

    def test_worker_isolates_devices_and_updates_only_matching_esp_images(self):
        self.installation()
        self.file('/boot/initrd.img-7.0-test',b'new initrd')
        with tempfile.TemporaryDirectory() as tmp:
            esp=Path(tmp);copy=esp/'EFI/proxmox/7.0-test/initrd.img'
            copy.parent.mkdir(parents=True);copy.write_bytes(b'old initrd')
            unrelated=esp/'EFI/proxmox/6.0-test/initrd.img'
            unrelated.parent.mkdir(parents=True);unrelated.write_bytes(b'keep')
            payload=dict(mounts=[dict(source='restore_rpool/ROOT/ubuntu',target='/'),
                                 dict(source='restore_bpool/BOOT/ubuntu',target='/boot')],
                         aliases=['restore_rpool','restore_bpool'],esps=['/dev/target1'])
            with patch.object(z,'recovery_directory',return_value=z.contextlib.nullcontext(self.root)), \
                 patch.object(z,'run',return_value='') as run,patch.object(z.os,'sync'), \
                 patch.object(z,'mounted',return_value=z.contextlib.nullcontext(esp)) as mounted, \
                 patch.object(z,'rebuild_restored_dracut',return_value=['7.0-test']):
                z.refresh_restored_dracut_worker(payload)
            self.assertEqual(copy.read_bytes(),b'new initrd')
            self.assertEqual(unrelated.read_bytes(),b'keep')
            mounted.assert_called_once_with('/dev/target1','rw')
        calls=[c.args for c in run.call_args_list]
        self.assertFalse(any('--rbind' in c for c in calls))
        binds=[c[2] for c in calls if c[:2]==('mount','--bind')]
        self.assertEqual(binds,['/dev/null','/dev/zero','/dev/random','/dev/urandom'])
        mounted_targets=[c[-1] for c in calls if c[0]=='mount']
        unmounted_targets=[c[-1] for c in calls if c[0]=='umount']
        self.assertEqual(unmounted_targets,list(reversed(mounted_targets)))

    def test_worker_unmounts_on_generation_failure(self):
        self.installation()
        with patch.object(z,'recovery_directory',return_value=z.contextlib.nullcontext(self.root)), \
             patch.object(z,'run',return_value='') as run, \
             patch.object(z,'rebuild_restored_dracut',side_effect=z.Error('bad initrd')), \
             self.assertRaisesRegex(z.Error,'bad initrd'):
            z.refresh_restored_dracut_worker(dict(mounts=[dict(source='restore_rpool/ROOT/ubuntu',target='/')],
                                                aliases=['restore_rpool']))
        calls=[c.args for c in run.call_args_list]
        self.assertEqual([c[-1] for c in calls if c[0]=='umount'],
                         list(reversed([c[-1] for c in calls if c[0]=='mount'])))


class BootPreparationLoggingTests(unittest.TestCase):
    setUp=InitramfsRepairTests.setUp
    file=InitramfsRepairTests.file
    installation=InitramfsRepairTests.installation

    def payload(self):
        return dict(mounts=[dict(source='target/ROOT/ubuntu',target='/')],aliases=['target'])

    def test_boot_timings_include_rebuild_without_fingerprinting(self):
        self.installation()
        clock=[0.0]
        def rebuild(root):
            self.assertIn('Rebuild and validate Dracut initramfs…',output.getvalue())
            clock[0]+=15
            return ['7.0-test']
        with patch.object(z,'recovery_directory',return_value=z.contextlib.nullcontext(self.root)), \
             patch.object(z,'run',return_value=''),patch.object(z.os,'sync'), \
             patch.object(z.time,'monotonic',side_effect=lambda:clock[0]), \
             patch.object(z,'rebuild_restored_dracut',side_effect=rebuild), \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            payload=self.payload();payload['timeline_started']=0
            with z.timed_phase('Boot preparation total'):
                z.refresh_restored_dracut_worker(payload)
        text=output.getvalue()
        self.assertIn('Rebuild and validate Dracut initramfs: 00:00:15',text)
        self.assertNotIn('Fingerprint',text)
        self.assertIn('Boot preparation total: 00:00:15',text)
        self.assertIn('Dracut boot preparation completed: 1 initramfs image(s) rebuilt',text)
        for step in ('Update and verify ESP initramfs copies','Flush boot preparation writes',
                     'Unmount boot preparation filesystems'):
            self.assertIn(step+'…',text);self.assertIn(step+': 00:00:00',text)
        for line in text.splitlines():
            self.assertTrue(line.startswith('['));self.assertEqual(line.count('['),1)

    def test_no_dracut_reports_skipped_without_completed_claim(self):
        self.installation(dracut=False)
        with patch.object(z,'recovery_directory',return_value=z.contextlib.nullcontext(self.root)), \
             patch.object(z,'run',return_value=''),patch.object(z,'rebuild_restored_dracut') as rebuild, \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            z.refresh_restored_dracut_worker(self.payload())
        self.assertIn('Dracut boot preparation skipped: Dracut is not installed',output.getvalue())
        self.assertNotIn('Dracut boot preparation completed',output.getvalue())
        rebuild.assert_not_called()

    def test_existing_reuse_record_is_ignored_and_boot_inputs_are_not_hashed(self):
        self.installation()
        self.file('/boot/.lllzorb-dracut.json',b'old reuse record')
        with patch.object(z,'recovery_directory',return_value=z.contextlib.nullcontext(self.root)), \
             patch.object(z,'run',return_value=''),patch.object(z.os,'sync'), \
             patch.object(z,'digest',side_effect=AssertionError('Unexpected input fingerprinting')) as digest, \
             patch.object(z,'rebuild_restored_dracut',return_value=['7.0-test']) as rebuild, \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            z.refresh_restored_dracut_worker(self.payload())
        self.assertIn('Dracut boot preparation completed: 1 initramfs image(s) rebuilt',output.getvalue())
        rebuild.assert_called_once_with(self.root)
        digest.assert_not_called()

    def test_failed_rebuild_logs_cleanup_but_no_completion(self):
        self.installation()
        with patch.object(z,'recovery_directory',return_value=z.contextlib.nullcontext(self.root)), \
             patch.object(z,'run',return_value=''), \
             patch.object(z,'rebuild_restored_dracut',side_effect=z.Error('build failed')), \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            with self.assertRaisesRegex(z.Error,'build failed'):
                z.refresh_restored_dracut_worker(self.payload())
        self.assertIn('Rebuild and validate Dracut initramfs: failed after',output.getvalue())
        self.assertIn('Unmount boot preparation filesystems:',output.getvalue())
        self.assertNotIn('Dracut boot preparation completed',output.getvalue())

    def test_live_commands_inherit_output_and_propagate_failure(self):
        for code in (0,1):
            result=z.subprocess.CompletedProcess(['worker'],code,stdout=None,stderr=None)
            with self.subTest(code=code),patch.object(z.subprocess,'run',return_value=result) as run:
                if code:
                    with self.assertRaisesRegex(z.Error,'exit status 1'):z.run('worker',live=True,input=b'{}')
                else:self.assertEqual(z.run('worker',live=True,input=b'{}'),'')
                self.assertIsNone(run.call_args.kwargs['stdout'])
                self.assertIsNone(run.call_args.kwargs['stderr'])
                self.assertEqual(run.call_args.kwargs['input'],b'{}')


class CloneRestoreTests(unittest.TestCase):
    def setUp(self):
        inventory=patch.object(z,'native_dataset_names',side_effect=lambda pool,remote=None:z.native_expected_names(pool))
        inventory.start();self.addCleanup(inventory.stop)

    def test_restore_uses_alias_and_refreshes_initramfs_without_legacy_boot_repair(self):
        m=native_fixture();plan=z.solve(m,500*z.GIB,512)
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);(base/'disk').mkdir();(base/'disk/first-megabyte.bin').write_bytes(b'B'*1024)
            target=base/'target';target.write_bytes(b'X'*1024)
            with patch.object(z,'create_layout'),patch.object(z,'partition_device',side_effect=lambda d,n:f'{d}-p{n}'), \
                 patch.object(z,'mounted',side_effect=lambda *a:z.contextlib.nullcontext(base)), \
                 patch.object(z,'run',return_value='ONLINE') as run,patch.object(z,'pipe_transfer') as transfer, \
                 patch.object(z,'restore_mount_properties') as properties,patch.object(z,'transfer_pool_guid') as guid, \
                 patch.object(z,'refresh_restored_dracut') as refresh, patch.object(z,'repair_boot') as repair:
                z.clone_restore(base,m,str(target),plan,[])
            repair.assert_not_called()
            refresh.assert_called_once()
            self.assertEqual(refresh.call_args.args[0],m)
            alias=guid.call_args.args[0]
            self.assertTrue(alias.startswith('restore_'))
            created=next(c.args for c in run.call_args_list if c.args[:2]==('zpool','create'))
            altroot=Path(created[created.index('-R')+1])
            self.assertTrue(altroot.is_absolute())
            self.assertTrue(altroot.name.startswith('lllzorb-target-'))
            self.assertFalse(altroot.exists())
            self.assertEqual(guid.call_args.args[1],'123')
            self.assertEqual(transfer.call_args.args[1][-1],alias)
            self.assertIn(alias+'/ROOT/ubuntu',properties.call_args.args[0]['datasets'])
            self.assertFalse(any(c.args[0] in ('chroot','efibootmgr') for c in run.call_args_list))
            self.assertEqual(target.read_bytes(),b'B'*440+b'X'*(1024-440))

    def test_same_host_restore_keeps_new_target_identity_and_exports_only_target(self):
        for boot_failure in (False,True):
            m=native_fixture();plan=z.solve(m,500*z.GIB,512)
            with tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);(base/'disk').mkdir();(base/'disk/first-megabyte.bin').write_bytes(b'B'*1024)
                target=base/'target';target.write_bytes(b'X'*1024)
                imported={'tank':'123','backup':'999'};created=[]
                def execute(*args,**kwargs):
                    if args[:2]==('zpool','create'):
                        alias=args[args.index('-t')+1];imported[alias]='456';created.append(alias)
                    if args[:2]==('zpool','get'):return imported[args[-1]]
                    if args[:2]==('zpool','list'):
                        return 'ONLINE' if 'health' in args else '\n'.join(imported)
                    if args[:2]==('zpool','export'):
                        self.assertIn(args[-1],created);del imported[args[-1]]
                    self.assertNotEqual(args[:2],('zpool','reguid'))
                    return ''
                def boot(manifest,restored,*args):
                    self.assertEqual(restored[0][0]['guid'],'123')
                    self.assertEqual(restored[0][0]['_restore_guid'],'456')
                    if boot_failure:raise z.Error('boot preparation failed')
                with self.subTest(boot_failure=boot_failure),patch.object(z,'create_layout'), \
                     patch.object(z,'partition_device',side_effect=lambda d,n:f'{d}-p{n}'), \
                     patch.object(z,'mounted',side_effect=lambda *a:z.contextlib.nullcontext(base)), \
                     patch.object(z,'run',side_effect=execute),patch.object(z,'pipe_transfer',return_value=1024), \
                     patch.object(z,'restore_mount_properties'),patch.object(z,'refresh_restored_dracut',side_effect=boot), \
                     patch.object(z,'transfer_pool_guid') as handoff:
                    if boot_failure:
                        with self.assertRaisesRegex(z.Error,'boot preparation failed'):
                            z.clone_restore(base,m,str(target),plan,[dict(name='tank',guid='123')])
                    else:z.clone_restore(base,m,str(target),plan,[dict(name='tank',guid='123')])
                    handoff.assert_not_called()
                self.assertEqual(imported,{'tank':'123','backup':'999'})

class SameHostBootTests(unittest.TestCase):
    setUp=InitramfsRepairTests.setUp
    file=InitramfsRepairTests.file
    installation=InitramfsRepairTests.installation
    old='9432379767224940307'
    new='13565230987966549120'

    def test_rewrites_both_guid_formats_but_preserves_signed_efi_and_other_files(self):
        oldhex=f'{int(self.old):016x}';newhex=f'{int(self.new):016x}'
        original=f'search --fs-uuid --set=root {oldhex}\nroot=ZFS={self.old}/ROOT/ubuntu\n'
        config=self.file('/boot/grub/grub.cfg',original.encode())
        defaults=self.file('/etc/default/grub.d/zfs.cfg',original.encode())
        other=self.file('/var/log/old-guid',self.old.encode())
        z.rewrite_boot_pool_guids(self.root,{self.old:self.new})
        expected=original.replace(oldhex,newhex).replace(self.old,self.new).encode()
        self.assertEqual(config.read_bytes(),expected);self.assertEqual(defaults.read_bytes(),expected)
        self.assertEqual(other.read_bytes(),self.old.encode())
        esp=self.root/'esp'
        cfg=self.file('/esp/EFI/ubuntu/grub.cfg',original.encode())
        loader=self.file('/esp/loader/entries/proxmox.conf',original.encode())
        signed=self.file('/esp/EFI/ubuntu/grubx64.efi',b'signed\0'+oldhex.encode())
        z.rewrite_boot_pool_guids(esp,{self.old:self.new},esp=True)
        self.assertEqual(cfg.read_bytes(),expected);self.assertEqual(loader.read_bytes(),expected)
        self.assertEqual(signed.read_bytes(),b'signed\0'+oldhex.encode())

    def test_does_not_replace_substrings_or_cascade_swapped_guids(self):
        a=f'{int(self.old):016x}';b=f'{int(self.new):016x}'
        cfg=self.file('/boot/grub/grub.cfg',f'{a} {b} 0{a} {a}0'.encode())
        z.rewrite_boot_pool_guids(self.root,{self.old:self.new,self.new:self.old})
        self.assertEqual(cfg.read_text(),f'{b} {a} 0{a} {a}0')

    def test_rejects_configuration_symlink_outside_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside=Path(tmp)/'grub.cfg';outside.write_text(self.old)
            (self.root/'boot/grub').mkdir(parents=True)
            (self.root/'boot/grub/grub.cfg').symlink_to(outside)
            with self.assertRaisesRegex(z.Error,'escapes installation'):
                z.rewrite_boot_pool_guids(self.root,{self.old:self.new})
            self.assertEqual(outside.read_text(),self.old)

    def test_initramfs_tools_rebuild_checks_all_images_before_replacing_any(self):
        self.installation(dracut=False)
        self.file('/usr/bin/mkinitramfs',executable=True)
        saved_config=self.file('/etc/initramfs-tools/initramfs.conf',b'MODULES=dep\n')
        self.file('/etc/initramfs-tools/conf.d/z-local',b'MODULES=dep\n')
        first=self.file('/boot/initrd.img-6.8-test',b'saved')
        cache=self.file('/etc/zfs/zpool.cache',b'stale')
        fail=[True]
        def execute(*args):
            self.assertFalse(cache.exists())
            if args[2].endswith('/mkinitramfs'):
                config=self.root/args[args.index('-d')+1].lstrip('/')
                self.assertEqual((config/'conf.d/z-local-restore').read_text(),'MODULES=most\n')
                self.file(args[-2],('new '+args[-1]).encode());return ''
            if fail[0] and '7.0-test' in (self.root/args[-1].lstrip('/')).read_text():
                return 'etc/zfs/zpool.cache'
            return 'lib/modules/zfs.ko.zst\nusr/sbin/zpool\nscripts/zfs\n'
        with patch.object(z,'run',side_effect=execute) as run:
            with self.assertRaisesRegex(z.Error,'ZFS boot support'):
                z.rebuild_restored_initramfs_tools(self.root)
            self.assertEqual(first.read_bytes(),b'saved')
            self.assertFalse(list((self.root/'boot').glob('.restore-initrd-*')))
            fail[0]=False
            self.assertEqual(z.rebuild_restored_initramfs_tools(self.root),['6.8-test','7.0-test'])
        self.assertEqual(first.read_bytes(),b'new 6.8-test')
        self.assertEqual(saved_config.read_text(),'MODULES=dep\n')
        self.assertFalse(list((self.root/'run').glob('restore-initramfs-*')))
        self.assertTrue(all(c.args[2].endswith(('/mkinitramfs','/lsinitramfs')) for c in run.call_args_list))

    def test_changed_identity_payload_uses_actual_guid_for_boot(self):
        m=ubuntu_fixture();restored=[]
        m['disk']['partitions'].append(dict(kind='bios_boot'))
        for i,pool in enumerate(m['pools']):
            pool['_restore_guid']=str(int(self.new)+i);restored.append((pool,'target_'+pool['name']))
        with patch.object(z,'restored_fstab_replacements',return_value={}),patch.object(z,'run',return_value='') as run,patch.object(z,'partition_device',return_value='/dev/disk/by-id/target-part1'):
            z.refresh_restored_dracut(m,restored,'/dev/disk/by-id/target',m['disk']['partitions'])
        payload=json.loads(run.call_args.kwargs['input'])
        self.assertEqual(payload['guid_changes'],{p['guid']:p['_restore_guid'] for p,_ in restored})
        self.assertEqual(payload['bios']['prefix'],'/BOOT/ubuntu_1opcom@/grub')
        self.assertEqual(payload['bios']['guid'],m['pools'][0]['_restore_guid'])

    def test_worker_updates_target_efi_configuration_without_dracut(self):
        self.installation(dracut=False)
        oldhex=f'{int(self.old):016x}';newhex=f'{int(self.new):016x}'
        cfg=self.file('/boot/grub/grub.cfg',oldhex.encode())
        initrd=self.file('/boot/initrd.img-7.0-test',b'new')
        esp=self.root/'esp';esp_cfg=self.file('/esp/EFI/ubuntu/grub.cfg',oldhex.encode())
        esp_initrd=self.file('/esp/EFI/proxmox/7.0-test/initrd.img',b'old')
        payload=dict(mounts=[dict(source='target/ROOT/os',target='/')],aliases=['target'],
                     guid_changes={self.old:self.new},esps=['/dev/disk/by-id/target-part1'])
        with patch.object(z,'recovery_directory',return_value=z.contextlib.nullcontext(self.root)), \
             patch.object(z,'run',return_value='') as run,patch.object(z.os,'sync'), \
             patch.object(z,'mounted',return_value=z.contextlib.nullcontext(esp)), \
             patch.object(z,'rebuild_restored_initramfs_tools',return_value=['7.0-test']) as rebuild:
            z.refresh_restored_dracut_worker(payload)
        rebuild.assert_called_once_with(self.root)
        self.assertEqual(cfg.read_text(),newhex);self.assertEqual(esp_cfg.read_text(),newhex)
        self.assertEqual(esp_initrd.read_bytes(),initrd.read_bytes())
        self.assertFalse(any(c.args[:2] in (('zpool','export'),('zpool','reguid')) for c in run.call_args_list))
        self.assertEqual([c.args[2] for c in run.call_args_list if c.args[:2]==('mount','--bind')],
                         ['/dev/null','/dev/zero','/dev/random','/dev/urandom'])

    def test_proxmox_bios_preserves_esp_boot_files(self):
        self.file('/boot/grub/grub.cfg',b'zfs config')
        self.file('/esp/grub/grub.cfg',b'fat config')
        (self.root/'esp/grub/i386-pc').mkdir()
        with patch.object(z,'run',return_value='ABCD-1234\n') as run:
            self.assertEqual(z.restored_bios_location(self.root,self.root/'esp','/dev/target-part2',
                             dict(guid=self.new,prefix='/ROOT/os@/boot/grub')),
                             ('/grub','ABCD-1234','fat'))
        run.assert_called_once_with('blkid','-p','-s','UUID','-o','value','/dev/target-part2')

    def test_proxmox_bios_rejects_missing_esp_identity(self):
        self.file('/esp/grub/grub.cfg',b'config')
        (self.root/'esp/grub/i386-pc').mkdir()
        with patch.object(z,'run',return_value=''):
            with self.assertRaisesRegex(z.Error,'ESP UUID'):
                z.restored_bios_location(self.root,self.root/'esp','/dev/target-part2',{})

    def test_bios_without_fat_modules_retains_zfs_boot_path(self):
        self.file('/boot/grub/grub.cfg',b'config')
        self.file('/esp/grub/grub.cfg',b'efi-only config')
        with patch.object(z,'run') as run:
            self.assertEqual(z.restored_bios_location(self.root,self.root/'esp','/dev/target-part2',
                             dict(guid=self.new,prefix='/ROOT/os@/boot/grub')),
                             ('/ROOT/os@/boot/grub',f'{int(self.new):016x}','zfs'))
        run.assert_not_called()

    def test_fat_bios_image_targets_selected_disk_only(self):
        self.test_bios_image_targets_selected_disk_only_and_cleans_up_on_failure(esp_boot=True)

    def test_bios_image_targets_selected_disk_only_and_cleans_up_on_failure(self,esp_boot=False):
        self.file('/usr/bin/grub-mkimage',executable=True)
        self.file('/usr/lib/grub/i386-pc/grub-bios-setup',executable=True)
        self.file('/usr/lib/grub/i386-pc/boot.img',b'boot')
        self.file('/boot/grub/grub.cfg',b'config')
        (self.root/'run').mkdir()
        device='/dev/disk/by-id/target';part=device+'-part1'
        real_stat=z.os.stat
        real_path=z.os.path.realpath
        def device_path(path,*args,**kwargs):
            return {device:'/dev/sdz',part:'/dev/sdz1'}.get(str(path)) or real_path(path,*args,**kwargs)
        def device_stat(path,*args,**kwargs):
            if str(path) in (device,part):return z.os.stat_result((z.stat.S_IFBLK,0,0,0,0,0,0,0,0,0))
            return real_stat(path,*args,**kwargs)
        with tempfile.TemporaryDirectory() as tmp:
            esp=Path(tmp)
            if esp_boot:
                (esp/'grub/i386-pc').mkdir(parents=True)
                (esp/'grub/grub.cfg').write_text('config')
            def execute(*args):
                if args[0]=='blkid':return 'ABCD-1234'
                if args[0]!='chroot':return ''
                stage=next(esp.glob('.restore-bios-*'))
                if args[2].endswith('grub-mkimage'):
                    self.assertIn('ABCD-1234' if esp_boot else f'{int(self.new):016x}',
                                  (stage/'load.cfg').read_text())
                    self.assertIn('fat' if esp_boot else 'zfs',args)
                    self.assertEqual(args[args.index('-p')+1],'/grub' if esp_boot else '/ROOT/os@/boot/grub')
                    (stage/'core.img').write_bytes(b'x'*1024)
                else:
                    self.assertEqual((stage/'device.map').read_text(),'(hd0) '+device+'\n')
                    self.assertEqual((self.root/device.lstrip('/')).resolve(),self.root/'dev/sdz')
                    self.assertEqual((self.root/part.lstrip('/')).resolve(),self.root/'dev/sdz1')
                    self.assertEqual(args[-1],'(hd0)')
                    raise z.Error('setup failed')
                return ''
            with patch.object(z.os,'stat',side_effect=device_stat),patch.object(z.os.path,'realpath',side_effect=device_path), \
                 patch.object(z,'run',side_effect=execute) as run:
                with self.assertRaisesRegex(z.Error,'setup failed'):
                    z.rebuild_restored_bios(self.root,esp,device,part,dict(guid=self.new,prefix='/ROOT/os@/boot/grub'))
            calls=[c.args for c in run.call_args_list]
            binds=[c for c in calls if c[:2]==('mount','--bind')]
            self.assertEqual([c[2] for c in binds[1:]],[device,part])
            self.assertEqual([c[-1] for c in calls if c[0]=='umount'],[c[-1] for c in reversed(binds)])
            self.assertEqual(list(esp.iterdir()),[esp/'grub'] if esp_boot else [])

    def test_efi_only_installation_does_not_require_zfs_grub_configuration(self):
        self.installation(dracut=False)
        self.file('/boot/initrd.img-7.0-test',b'initrd')
        esp=self.root/'esp';esp.mkdir()
        payload=dict(mounts=[dict(source='target/ROOT/os',target='/')],aliases=['target'],
                     guid_changes={self.old:self.new},esps=['/dev/disk/by-id/target-part1'],
                     bios=dict(guid=self.new,prefix='/ROOT/os@/boot/grub'),device='/dev/disk/by-id/target')
        with patch.object(z,'recovery_directory',return_value=z.contextlib.nullcontext(self.root)), \
             patch.object(z,'run',return_value=''),patch.object(z.os,'sync'), \
             patch.object(z,'mounted',return_value=z.contextlib.nullcontext(esp)), \
             patch.object(z,'rebuild_restored_initramfs_tools',return_value=['7.0-test']), \
             patch.object(z,'rebuild_restored_bios') as bios:
            z.refresh_restored_dracut_worker(payload)
        bios.assert_not_called()


class DiskOnlyInterfaceTests(unittest.TestCase):
    def test_backup_rejects_directory_without_storage_commands(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(z,'run') as run:
            with self.assertRaisesRegex(z.Error,'whole disk device'):
                with z.backup_destination(tmp,'/dev/source',1):
                    self.fail('Directory accepted')
            run.assert_not_called()

    def test_management_rejects_directory_without_storage_commands(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(z,'run') as run:
            with self.assertRaisesRegex(z.Error,'whole disk device'):
                with z.management_storage(tmp):
                    self.fail('Directory accepted')
            run.assert_not_called()

class ExactPartitionPositionTests(unittest.TestCase):
    def test_bios_partition_disables_rounding_before_creation(self):
        part=dict(number=1,start_lba=34,end_lba=2047,type_guid=z.BIOS,
                  name='',partuuid=str(uuid.uuid4()),attributes=0)
        with patch.object(z,'stable_device',side_effect=str), patch.object(z,'run') as run, \
             patch.object(z,'node_for',return_value={'log-sec':512,'size':z.GIB}), \
             patch.object(z,'read_gpt',return_value={'entry_count':128,'partitions':[part]}):
            z.create_layout('/dev/target',[part],128)
        command=next(c.args for c in run.call_args_list if '--new=1:34:2047' in c.args)
        self.assertLess(command.index('--set-alignment=1'),command.index('--new=1:34:2047'))
        self.assertEqual(command[-1],'/dev/target')

    def test_signature_cleanup_failure_stops_before_partition_creation(self):
        with patch.object(z,'stable_device',side_effect=str), \
             patch.object(z,'node_for',return_value={'log-sec':512,'size':z.GIB}), \
             patch.object(z,'run',side_effect=z.Error('wipe failed')) as run, \
             patch.object(z,'read_gpt') as read,self.assertRaisesRegex(z.Error,'wipe failed'):
            z.create_layout('/dev/disk/by-id/test',[],128)
        run.assert_called_once_with('wipefs','--all','--force','/dev/disk/by-id/test')
        read.assert_not_called()

    def test_real_tools_replace_blank_mbr_gpt_and_conflicting_tables(self):
        search=z.os.environ.get('PATH','')+':/usr/sbin:/sbin'
        binaries={name:z.shutil.which(name,path=search) for name in ('sgdisk','wipefs')}
        if not all(binaries.values()):self.skipTest('sgdisk and wipefs required for image test')
        size=1000204886016
        layout=[]
        for number,start,end,kind in ((1,2048,2203647,z.ESP),(2,2203648,6397951,z.LINUX_FS),
                                     (3,6397952,23175167,z.SWAP),(4,23175168,1953523711,z.LINUX_FS)):
            layout.append(dict(number=number,start_lba=start,end_lba=end,type_guid=kind,
                               name='',partuuid=str(uuid.uuid4()),attributes=0))
        for previous in ('blank','mbr','gpt','conflicting'):
            with self.subTest(previous=previous),tempfile.TemporaryDirectory() as tmp:
                image=Path(tmp)/'disk.img'
                with image.open('wb') as f:f.truncate(size)
                if previous in ('gpt','conflicting'):
                    result=z.subprocess.run([binaries['sgdisk'],'--clear','--new=1:2048:100000',str(image)],capture_output=True)
                    self.assertEqual(result.returncode,0,result.stderr)
                if previous in ('mbr','conflicting'):
                    mbr=bytearray(512)
                    struct.pack_into('<B3sB3sII',mbr,446,0,b'\x20\x21\x00',7,b'\xfe\xff\xff',2048,size//512-2048)
                    mbr[510:]=b'\x55\xaa'
                    with image.open('r+b') as f:f.write(mbr)
                with image.open('r+b') as f:f.seek(4*z.MIB);f.write(b'partition payload')
                execute=z.run;events=[]
                def run(*args,**kwargs):
                    events.append(args)
                    if args[0] in ('partprobe','udevadm'):return ''
                    self.assertEqual(args[-1],str(image))
                    return execute(binaries[args[0]],*args[1:],**kwargs)
                with patch.object(z,'stable_device',side_effect=str), \
                     patch.object(z,'node_for',return_value={'log-sec':512,'size':size}), \
                     patch.object(z,'run',side_effect=run):
                    z.create_layout(str(image),layout,128)
                actual=z.read_gpt(str(image),512,size)
                self.assertEqual(len(actual['partitions']),4)
                for expected,observed in zip(layout,actual['partitions']):
                    for key,value in expected.items():self.assertEqual(observed[key],value)
                self.assertEqual([event[0] for event in events],['wipefs','sgdisk','partprobe','udevadm'])
                with image.open('rb') as f:f.seek(4*z.MIB);self.assertEqual(f.read(17),b'partition payload')


def ubuntu_fixture():
    m=native_fixture()
    m['root_dataset']='rpool/ROOT/ubuntu_1opcom'
    m['mounts']=[dict(source=m['root_dataset'],target='/',fstype='zfs'),
                 dict(source='bpool/BOOT/ubuntu_1opcom',target='/boot',fstype='zfs')]
    m['disk'].update(size_bytes=128035676160,physical_sector_size=512)
    m['disk']['partitions']=[]
    for number,start,end,kind,guid in (
        (1,2048,2203647,'esp',z.ESP),
        (2,2203648,6397951,'zfs',z.LINUX_FS),
        (3,6397952,23175167,'swap',z.SWAP),
        (4,23175168,250066943,'zfs',z.LINUX_FS)):
        part=dict(number=number,start_lba=start,end_lba=end,size_bytes=(end-start+1)*512,
                  kind=kind,type_guid=guid,partuuid=str(uuid.uuid4()),name='',attributes=0,
                  source_device=f'/dev/sdj{number}')
        if kind=='esp':
            part.update(fat_bits=32,fat_uuid='C82B-E0E0',fat_label='',archive='efi/esp-1.tar.zst',mountpoints=['/boot/efi'])
        elif kind=='swap':
            part.update(swap_uuid='10dc89b7-3ed1-406f-8006-e7bfdb8aad5e',swap_label='',swap_version=1)
        else:
            part['pool']='bpool' if number==2 else 'rpool'
        m['disk']['partitions'].append(part)
    template=m['pools'][0];m['pools']=[]
    for name,number,guid,used,child in (
        ('bpool',2,'5705941148057882512',256*z.MIB,'BOOT/ubuntu_1opcom'),
        ('rpool',4,'13981170610164063270',10*z.GIB,'ROOT/ubuntu_1opcom')):
        pool=copy.deepcopy(template)
        datasets={name:{'encryption':{'value':'off'}},name+'/'+child:{}}
        for dataset in list(datasets):
            datasets[dataset+'@'+m['snapshot']]={'guid':{'value':guid}}
        pool.update(name=name,guid=guid,partitions=[number],leaves=[f'/dev/sdj{number}'],
                    bootfs=name+'/'+child,datasets=datasets,native_dataset=m['native_root']+'/'+name)
        for key in ('allocated_bytes','used_bytes','logicalused_bytes','referenced_bytes','estimated_send_bytes','stream_bytes'):
            pool[key]=used
        m['pools'].append(pool)
    m['boot']['fstab']='UUID=C82B-E0E0 /boot/efi vfat defaults 0 1\nUUID=10dc89b7-3ed1-406f-8006-e7bfdb8aad5e none swap sw 0 0\n'
    return m


class ExistingLayoutDisplayTests(unittest.TestCase):
    def test_partition_inventory_shows_sizes_positions_types_and_labels(self):
        disk=dict(path='/dev/sdb',size=100*z.GIB,type='disk',pttype='gpt',children=[
            dict(path='/dev/sdb1',size=512*z.MIB,start=2048,type='part',fstype='vfat',label='OLD EFI',
                 partlabel='EFI system',parttype=z.ESP),
            dict(path='/dev/sdb2',size=90*z.GIB,start=1050624,type='part',fstype='ext4',label='old-files',
                 partlabel='Old root',parttype=z.LINUX_FS)])
        original=copy.deepcopy(disk)
        with patch('sys.stdout',new_callable=io.StringIO) as output,patch.object(z,'run') as run:
            z.print_existing_layout('/dev/sdb',disk)
        text=output.getvalue()
        for value in ('EXISTING TARGET LAYOUT: /dev/sdb','gpt','/dev/sdb1','/dev/sdb2',
                      '0.500 GiB','90.000 GiB','2048','1050624','vfat','ext4','OLD EFI','Old root',z.ESP):
            self.assertIn(value,text)
        self.assertLess(text.index('/dev/sdb1'),text.index('/dev/sdb2'))
        self.assertEqual(disk,original)
        run.assert_not_called()

    def test_unpartitioned_filesystem_is_visible_and_missing_fields_work(self):
        for disk in (dict(path='/dev/sdb',size=z.GIB,type='disk',fstype='ext4',label='whole-disk'),
                     dict(size=z.GIB,children=None)):
            with self.subTest(disk=disk),patch('sys.stdout',new_callable=io.StringIO) as output:
                z.print_existing_layout('/dev/sdb',disk)
            self.assertIn('No partitions detected',output.getvalue())
            if disk.get('fstype'):
                self.assertIn('ext4',output.getvalue());self.assertIn('whole-disk',output.getvalue())


class DiscardSelectionTests(unittest.TestCase):
    def test_explicit_choice_and_unattended_default_never_prompt(self):
        for choice,unattended,expected in [('on',False,True),('off',False,False),
                                          (None,True,False),(None,False,False),('off',True,False)]:
            with self.subTest(choice=choice,unattended=unattended),patch('builtins.input') as prompt:
                args=z.argparse.Namespace(discard=choice,unattended=unattended)
                self.assertEqual(z.choose_restore_discard(args),expected)
                prompt.assert_not_called()

    def test_discard_failure_aborts(self):
        with patch.object(z,'commands'),patch.object(z,'run',side_effect=z.Error('I/O error')):
            with self.assertRaisesRegex(z.Error,'I/O error'):z.discard_restore_target('/dev/target')

    def test_incremental_discard_on_rejected_before_storage_access(self):
        with patch.object(z,'commands') as commands:
            with self.assertRaisesRegex(z.Error,'cannot be used with incremental'):
                z.restore(z.argparse.Namespace(incremental=True,discard='on'))
            commands.assert_not_called()

    def test_cli_passes_discard_choices_and_rejects_other_values(self):
        for choice in ('on','off'):
            with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'restore') as restore, \
                 patch('sys.stdout',new_callable=io.StringIO):
                self.assertEqual(z.main(['restore','--discard',choice],restore_isolated=True),0)
                self.assertEqual(restore.call_args.args[0].discard,choice)
        with patch('sys.stderr',new_callable=io.StringIO),self.assertRaises(SystemExit) as exc:
            z.main(['restore','--discard','yes'])
        self.assertEqual(exc.exception.code,2)


class SwapResizeTests(unittest.TestCase):
    def test_no_swap_does_not_prompt(self):
        with patch('builtins.input',side_effect=AssertionError('unexpected prompt')):
            self.assertEqual(z.choose_restore_swap_sizes(fixture()),{})

    def test_enter_preserves_exact_original_size(self):
        m=ubuntu_fixture()
        for original in (8*z.GIB,8*z.GIB+512):
            m['disk']['partitions'][2]['size_bytes']=original
            with patch('builtins.input',return_value='') as prompt,patch('sys.stdout',new_callable=io.StringIO):
                self.assertEqual(z.choose_restore_swap_sizes(m),{3:original})
            self.assertIn('size in MB [8192',prompt.call_args.args[0])

    def test_original_sub_mib_swap_can_still_be_preserved(self):
        m=ubuntu_fixture();m['disk']['partitions'][2]['size_bytes']=64*1024
        with patch('builtins.input',return_value=''),patch('sys.stdout',new_callable=io.StringIO):
            sizes=z.choose_restore_swap_sizes(m)
        self.assertEqual(z.solve(m,64*z.GIB,512,swap_sizes=sizes)['partitions'][2]['size_bytes'],64*1024)

    def test_invalid_values_reprompt(self):
        with patch('builtins.input',side_effect=['garbage','-1','0','8192.5','32768']) as prompt, \
             patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(z.choose_restore_swap_sizes(ubuntu_fixture()),{3:32*z.GIB})
        self.assertEqual(prompt.call_count,5)

    def test_shrinking_frees_space_for_root_and_preserves_swap_identity(self):
        m=ubuntu_fixture();original=copy.deepcopy(m)
        before=z.solve(m,64*z.GIB,512)
        for size in (1,4096):
            with patch('builtins.input',return_value=str(size)),patch('sys.stdout',new_callable=io.StringIO):
                sizes=z.choose_restore_swap_sizes(m)
            after=z.solve(m,64*z.GIB,512,swap_sizes=sizes)
            self.assertEqual(after['partitions'][2]['size_bytes'],size*z.MIB)
            self.assertEqual(after['partitions'][3]['size_bytes']-before['partitions'][3]['size_bytes'],8*z.GIB-size*z.MIB)
            for key in ('swap_uuid','swap_label','partuuid','number'):
                self.assertEqual(after['partitions'][2][key],m['disk']['partitions'][2][key])
            self.assertTrue(all(a['end_lba']<b['start_lba'] for a,b in zip(after['partitions'],after['partitions'][1:])))
        self.assertEqual(m,original)

    def test_multiple_swap_partitions_prompt_separately(self):
        m=ubuntu_fixture();extra=copy.deepcopy(m['disk']['partitions'][2])
        extra.update(number=5,start_lba=300000000,size_bytes=2*z.GIB)
        m['disk']['partitions'].append(extra)
        with patch('builtins.input',side_effect=['32768','']) as prompt,patch('sys.stdout',new_callable=io.StringIO):
            self.assertEqual(z.choose_restore_swap_sizes(m),{3:32*z.GIB,5:2*z.GIB})
        self.assertIn('p3',prompt.call_args_list[0].args[0])
        self.assertIn('p5',prompt.call_args_list[1].args[0])

    def test_expansion_reduces_root_preserves_identity_and_manifest(self):
        m=ubuntu_fixture();original=copy.deepcopy(m)
        before=z.solve(m,64*z.GIB,512)
        after=z.solve(m,64*z.GIB,512,swap_sizes={3:32*z.GIB})
        self.assertEqual(m,original)
        self.assertEqual(after['partitions'][2]['size_bytes'],32*z.GIB)
        self.assertEqual(before['partitions'][3]['size_bytes']-after['partitions'][3]['size_bytes'],24*z.GIB)
        for a,b in zip(before['partitions'],after['partitions']):
            self.assertEqual(a['number'],b['number'])
            self.assertEqual(a['partuuid'],b['partuuid'])
        self.assertEqual(after['partitions'][2]['swap_uuid'],original['disk']['partitions'][2]['swap_uuid'])
        self.assertEqual(before['partitions'][:2],after['partitions'][:2])
        self.assertEqual(before['partitions'][-1]['end_lba'],after['partitions'][-1]['end_lba'])
        self.assertTrue(all(a['end_lba']<b['start_lba'] for a,b in zip(after['partitions'],after['partitions'][1:])))
        self.assertEqual(after['minimum_bytes']-before['minimum_bytes'],24*z.GIB)

    def test_solver_rejects_bad_overrides(self):
        for sizes in ({1:32*z.GIB},{3:0},{3:-z.MIB},{3:512},{3:8*z.GIB+1},{3:'32768'}):
            with self.subTest(sizes=sizes),self.assertRaises(z.Error):
                z.solve(ubuntu_fixture(),64*z.GIB,512,swap_sizes=sizes)

    def invoke_restore(self,answer,dry=False,remote=None,too_large=False,discard='off',shortfall=False,capacity_answer='yes'):
        m=ubuntu_fixture()
        if shortfall:
            m['pools'][-1]['stream_bytes']=m['pools'][-1]['estimated_send_bytes']=122*z.GIB
        original=copy.deepcopy(m)
        declined=shortfall and not dry and capacity_answer not in ('yes','y')
        args=z.argparse.Namespace(target='/dev/target',dry_run=dry,discard=discard)
        disk={'size':64*z.GIB,'log-sec':512,'phy-sec':512,'model':'test',
              'children':[{'path':'/dev/target1','fstype':'ext4','label':'old-files'}]}
        def confirm_target(device):
            self.assertIsNone(z.DIAGNOSTIC_STARTED)
            if shortfall:self.assertEqual(prompt.call_count,2)
            self.assertIn('old-files',output.getvalue())
            self.assertLess(output.getvalue().index('EXISTING TARGET LAYOUT'),
                            output.getvalue().index('PROPOSED TARGET LAYOUT'))
        def execute(*args,**kwargs):
            if args[:2]==('zpool','reguid'):return '-g'
            if args==('zfs','version'):return 'zfs-kmod-2.3.0'
            return ''
        with patch.object(z,'DIAGNOSTIC_STARTED',None), \
             patch.object(z,'stable_device',side_effect=str), patch.object(z,'select_backup',return_value=Path('/backup')), \
             patch.object(z,'verify_chain',return_value=[(Path('/backup'),m)]), \
             patch.object(z,'commands'),patch.object(z,'run',side_effect=execute), \
             patch.object(z,'estimate_native_send',side_effect=lambda p,*a,**kw:p['stream_bytes']), \
             patch.object(z,'protected_path',return_value=set()),patch.object(z,'target_idle',return_value=disk), \
             patch.object(z,'guid_conflicts',return_value=[]),patch.object(z,'confirm',side_effect=confirm_target) as confirm, \
             patch.object(z,'discard_restore_target') as discard_target, \
             patch.object(z,'clone_restore') as restore,patch('builtins.input',side_effect=[answer,capacity_answer]) as prompt, \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            if too_large:
                with self.assertRaisesRegex(z.Error,'Target too small'):
                    z.restore_from_storage(args,Path('/backup'),remote=remote)
            elif declined:
                with self.assertRaisesRegex(z.Error,'not accepted'):
                    z.restore_from_storage(args,Path('/backup'),remote=remote)
            else:z.restore_from_storage(args,Path('/backup'),remote=remote)
            if dry or too_large or declined:self.assertIsNone(z.DIAGNOSTIC_STARTED)
            else:self.assertIsNotNone(z.DIAGNOSTIC_STARTED)
        self.assertEqual(prompt.call_count,2 if shortfall and not dry and not too_large else 1)
        if discard=='on' and not dry and not too_large and not declined:
            discard_target.assert_called_once_with('/dev/target')
        else:
            discard_target.assert_not_called()
        self.assertEqual(m,original)
        if not too_large:
            self.assertIn('old-files',output.getvalue())
            self.assertLess(output.getvalue().index('EXISTING TARGET LAYOUT'),
                            output.getvalue().index('PROPOSED TARGET LAYOUT'))
        if dry or too_large or declined:
            confirm.assert_not_called();restore.assert_not_called()
        else:
            confirm.assert_called_once_with('/dev/target')
            restore.assert_called_once()
            self.assertEqual(restore.call_args.args[3]['partitions'][2]['size_bytes'],32*z.GIB)
            self.assertEqual(restore.call_args.args[1],original)

    def test_capacity_confirmation_precedes_destruction_and_discard(self):
        for remote in (None,object()):
            with self.subTest(remote=bool(remote)):
                self.invoke_restore('32768',remote=remote,shortfall=True,discard='on')
                self.invoke_restore('32768',remote=remote,shortfall=True,discard='on',capacity_answer='n')
                self.invoke_restore('32768',remote=remote,shortfall=True,discard='on',dry=True)

    def test_local_and_remote_restore_use_selected_size(self):
        for remote in (None,object()):
            with self.subTest(remote=bool(remote)):self.invoke_restore('32768',remote=remote)

    def test_discard_on_runs_only_after_preflight_and_confirmation(self):
        self.invoke_restore('32768',discard='on')
        self.invoke_restore('32768',dry=True,discard='on')
        self.invoke_restore('65536',too_large=True,discard='on')

    def test_dry_run_prompts_without_erasing(self):
        self.invoke_restore('32768',dry=True)

    def test_oversized_swap_fails_before_destructive_confirmation(self):
        self.invoke_restore('65536',too_large=True)


class UbuntuLayoutTests(unittest.TestCase):
    def test_manifest_accepts_two_pools_linux_guid_and_swap(self):
        self.assertEqual(z.validate(ubuntu_fixture()),{'efi/esp-1.tar.zst'})

    def test_fixed_boot_and_swap_root_fills_smaller_and_larger_targets(self):
        m=ubuntu_fixture()
        for size in (64*z.GIB,256*z.GIB):
            plan=z.solve(m,size,512);parts=plan['partitions']
            self.assertEqual([p['number'] for p in parts],[1,2,3,4])
            for original,new in zip(m['disk']['partitions'][:3],parts[:3]):
                self.assertEqual(new['size_bytes'],original['size_bytes'])
                self.assertEqual(new['partuuid'],original['partuuid'])
            self.assertEqual(parts[2]['swap_uuid'],m['disk']['partitions'][2]['swap_uuid'])
            self.assertTrue(all(a['end_lba']<b['start_lba'] for a,b in zip(parts,parts[1:])))
            end=(size-512-m['disk']['entry_count']*128)//z.MIB*z.MIB
            self.assertEqual((parts[-1]['end_lba']+1)*512,end)
            self.assertEqual(set(plan['pools']),{'bpool','rpool'})

    def test_insufficient_fixed_boot_capacity_rejected(self):
        m=ubuntu_fixture();m['pools'][0]['stream_bytes']=3*z.GIB
        with self.assertRaisesRegex(z.Error,'boot-pool partition'):
            z.solve(m,256*z.GIB,512)

    def test_root_capacity_includes_swap_and_boot_pool(self):
        with self.assertRaisesRegex(z.Error,'Target too small'):
            z.solve(ubuntu_fixture(),20*z.GIB,512)

    def test_unrelated_second_pool_rejected(self):
        m=ubuntu_fixture();m['mounts'][1]['target']='/data'
        with self.assertRaisesRegex(z.Error,'second ZFS pool'):
            z.validate(m)

    def test_swap_metadata_rejected_when_invalid(self):
        for key,value in (('swap_uuid','bad'),('swap_label','x'*16),('swap_version',0)):
            m=ubuntu_fixture();m['disk']['partitions'][2][key]=value
            with self.subTest(key=key),self.assertRaises((z.Error,ValueError)):
                z.validate(m)

    def test_both_pool_base_snapshots_checked(self):
        m=ubuntu_fixture();current=copy.deepcopy(m)
        guids={p['name']:p['guid'] for p in m['pools']}
        with patch.object(z,'catalog',return_value=[(Path('/repo/base'),m)]), \
             patch.object(z,'verify_chain'),patch.object(z,'run',side_effect=lambda *a,**k:guids[a[-1].split('/')[0].split('@')[0]]) as run:
            self.assertEqual(z.incremental_parent('/repo',current,native_only=True)[0],Path('/repo/base'))
        self.assertEqual(len(run.call_args_list),4)


class UbuntuDiscoveryTests(unittest.TestCase):
    def discover(self,wrong_fs=False):
        m=ubuntu_fixture();geometry=copy.deepcopy(m['disk']);nodes={}
        for part in geometry['partitions']:
            n=dict(type='part',path=part['source_device'],fstype={'esp':'vfat','swap':'swap','zfs':'zfs_member'}[part['kind']],
                   uuid=part.get('fat_uuid'),label='',mountpoints=part.get('mountpoints',[]))
            if wrong_fs and part['number']==2:n['fstype']='ext4'
            nodes[n['path']]=n
        nodes['/dev/sdj']=dict(type='disk',path='/dev/sdj',size=geometry['size_bytes'],
                               **{'log-sec':512,'phy-sec':512,'children':list(nodes.values())})
        pools={p['name']:p for p in m['pools']}
        def run(*args,**kwargs):
            if args[:2]==('findmnt','-J'):
                return json.dumps({'filesystems':m['mounts'][:1] if '-T' in args else m['mounts']})
            if args[:2]==('zpool','list'):return 'bpool\nrpool\n'
            if args[0]=='blkid':
                if args[-1]=='/dev/sdj3':return 'TYPE=swap\nVERSION=1\nUUID='+m['disk']['partitions'][2]['swap_uuid']+'\n'
                return 'LABEL='+('bpool' if args[-1]=='/dev/sdj2' else 'rpool')+'\n'
            raise AssertionError(args)
        def props(program,name,recursive=False):
            pool=pools[name]
            if program=='zfs':
                ds=copy.deepcopy(pool['datasets'])
                ds[name].update({k:{'value':str(pool[k+'_bytes'])} for k in ('used','logicalused','referenced')})
                return ds
            return {name:{'guid':{'value':pool['guid']},'allocated':{'value':str(pool['allocated_bytes'])},'bootfs':{'value':pool['bootfs']}}}
        boot=bytearray(512);boot[510:]=b'\x55\xaa';struct.pack_into('<H',boot,11,512)
        boot[13]=8;struct.pack_into('<H',boot,14,32);boot[16]=2
        struct.pack_into('<I',boot,32,2201600);struct.pack_into('<I',boot,36,2048)
        with patch.object(z,'run',side_effect=run),patch.object(z,'read_gpt',return_value=geometry), \
             patch.object(z,'node_for',side_effect=lambda d:nodes[d]),patch.object(z,'disk_for',return_value='/dev/sdj'), \
             patch.object(z,'pool_leaves',side_effect=lambda p:pools[p]['leaves']), \
             patch.object(z,'partition_device',side_effect=lambda d,n:f'/dev/sdj{n}'), \
             patch.object(z,'signatures',side_effect=lambda d:[{'type':'gpt'}] if d=='/dev/sdj' else [{'type':nodes[d]['fstype']}]), \
             patch.object(z,'props',side_effect=props),patch.object(z,'pool_ashift',return_value=12), \
             patch('builtins.open',side_effect=lambda *a,**k:io.BytesIO(boot)):
            return z.discover()

    def test_discovers_users_partition_layout(self):
        m=self.discover()
        self.assertEqual([p['kind'] for p in m['disk']['partitions']],['esp','zfs','swap','zfs'])
        self.assertEqual([p['name'] for p in m['pools']],['bpool','rpool'])
        self.assertEqual(m['disk']['partitions'][2]['swap_uuid'],'10dc89b7-3ed1-406f-8006-e7bfdb8aad5e')

    def test_linux_filesystem_guid_does_not_allow_ext4(self):
        with self.assertRaisesRegex(z.Error,'Invalid ZFS partition'):
            self.discover(wrong_fs=True)


class UbuntuRestoreTests(unittest.TestCase):
    def setUp(self):
        inventory=patch.object(z,'native_dataset_names',side_effect=lambda pool,remote=None:z.native_expected_names(pool))
        inventory.start();self.addCleanup(inventory.stop)

    def exercise(self,fail_receive=False,fail_guid=False,remote_mode=False,fail_inventory=False,fail_refresh=False,compression=None):
        m=ubuntu_fixture();plan=z.solve(m,64*z.GIB,512)
        m['pools'][1]['properties']['feature@zstd_compress']={'value':'enabled'}
        z.restore_compression(m,compression)
        remote=z.RemoteRepository('backup@server') if remote_mode else None
        if remote:
            remote.dataset='store'
            remote.journal=lambda:Path('/persistent-journals')/(z.uuid.uuid4().hex+'.json')
        active=set();events=[];commands=[];journals=[];ratio_reads=[]
        def run(*args,**kwargs):
            commands.append(args)
            if args[:2]==('zpool','create'):active.add(args[args.index('-t')+1])
            if args[:2]==('zpool','list'):
                return 'ONLINE' if 'health' in args else '\n'.join(active)
            if args[:2]==('zpool','export'):active.remove(args[-1])
            if args[:2]==('zfs','list') and args[-2]=='name,type,guid,createtxg,referenced,origin':
                root=args[-1]
                return f'{root}\tfilesystem\t1\t1\t0\t-\n{root}@point\tsnapshot\t2\t2\t{len(events)*300}\t-\n'
            if args[:2]==('zfs','list') and args[-2].endswith(',compressratio'):
                self.assertIn(args[-1],active)
                self.assertIn(('zpool','sync',args[-1]),commands)
                self.assertEqual(events,['Restore bpool','Restore rpool'])
                ratio_reads.append(args[-1])
                ratio='1.60' if len(ratio_reads)==1 else '2.40'
                return f'{args[-1]}\t{z.GIB}\t0\t{2*z.GIB}\t{ratio}\n'
            return ''
        def receive(sender,receiver,total,label):
            self.assertEqual(sender[0],'ssh' if remote else 'zfs')
            self.assertIn('-b',z.shlex.split(sender[-1]) if remote else sender)
            self.assertEqual(receiver[:2],['zfs','receive'])
            settings=[a for a in receiver if a.startswith('compression=')]
            if label=='Restore bpool':
                self.assertEqual(settings,[])
                self.assertFalse(any(c[:2]==('zfs','set') and c[-1]==receiver[-1] and
                                     any(a.startswith('compression=') for a in c) for c in commands))
            else:
                self.assertEqual(settings,['compression='+compression] if compression else [])
                self.assertIn(('zfs','set','compression='+(compression or 'lz4'),receiver[-1]),commands)
            events.append(label)
            if fail_receive and len(events)==2:raise z.Error('receive failed')
            return len(events)*1234
        def refresh_images(*args):
            self.assertEqual(events,['Restore bpool','Restore rpool'])
            if fail_refresh:raise z.Error('initramfs failed')
        def guid(temporary,original,conflict,journal):
            self.assertEqual(events[:2],['Restore bpool','Restore rpool'])
            events.append(original);journals.append(journal)
            if fail_guid and len(journals)==2:raise z.Error('GUID failed')
            active.remove(temporary)
        def inventory(pool,remote=None):
            names=z.native_expected_names(pool)
            if fail_inventory and pool['native_dataset'].startswith('restore_'):
                names.add(pool['native_dataset']+'/unexpected-vm')
            return names
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);(base/'disk').mkdir();(base/'disk/first-megabyte.bin').write_bytes(b'B'*1024)
            target=base/'target';target.write_bytes(b'X'*1024)
            with patch.object(z,'create_layout'),patch.object(z,'partition_device',side_effect=lambda d,n:f'{d}-p{n}'), \
                 patch.object(z,'mounted',side_effect=lambda *a:z.contextlib.nullcontext(base)), \
                 patch.object(z,'run',side_effect=run),patch.object(z,'pipe_transfer',side_effect=receive), \
                 patch.object(z,'restore_mount_properties'),patch.object(z,'transfer_pool_guid',side_effect=guid), \
                 patch.object(z,'native_dataset_names',side_effect=inventory), \
                 patch.object(z,'refresh_restored_dracut',side_effect=refresh_images) as refresh, patch.object(z,'repair_boot') as repair, \
                 patch('sys.stdout',new_callable=io.StringIO) as output:
                if fail_receive or fail_guid or fail_inventory or fail_refresh:
                    with self.assertRaises(z.Error):z.clone_restore(base,m,str(target),plan,[],remote=remote)
                else:
                    transferred=z.clone_restore(base,m,str(target),plan,[],remote=remote)
                    self.assertEqual(transferred.stream,3*1234)
                    self.assertEqual(transferred.uncompressed,3*1234)
                    self.assertEqual(transferred.compressed,900)
            repair.assert_not_called()
            if fail_receive or fail_inventory or fail_refresh:
                self.assertEqual(ratio_reads,[])
            else:
                self.assertEqual(len(ratio_reads),2)
                self.assertIn('bpool: compressed 1.000 GiB, uncompressed 2.000 GiB | ratio 1.60x',output.getvalue())
                self.assertIn('rpool: compressed 1.000 GiB, uncompressed 2.000 GiB | ratio 2.40x',output.getvalue())
        self.assertFalse(active)
        swap=next(c for c in commands if c[0]=='mkswap')
        self.assertIn('10dc89b7-3ed1-406f-8006-e7bfdb8aad5e',swap)
        self.assertTrue(swap[-1].endswith('-p3'))
        self.assertFalse(any(c[0] in ('swapon','swapoff','chroot','efibootmgr') for c in commands))
        self.assertFalse(any(c[0]=='ssh' for c in commands))
        self.assertFalse(any('compression=off' in c for c in commands))
        return events,journals

    def test_remote_restore_streams_both_pools_to_local_receivers_and_persistent_journals(self):
        events,journals=self.exercise(remote_mode=True)
        self.assertEqual(len(journals),2)
        self.assertTrue(all(p.parent==Path('/persistent-journals') for p in journals))

    def test_remote_second_receive_failure_cleans_local_pools_before_guid_handoff(self):
        events,journals=self.exercise(remote_mode=True,fail_receive=True)
        self.assertEqual(journals,[])

    def test_restores_both_pools_before_guid_handoffs_and_recreates_swap(self):
        events,journals=self.exercise()
        self.assertEqual(events[2:],['5705941148057882512','13981170610164063270'])
        self.assertEqual(len(set(journals)),2)

    def test_compression_override_only_applies_to_root_pool(self):
        self.exercise(compression='zstd-3')

    def test_second_receive_failure_exports_both_temporary_pools(self):
        events,journals=self.exercise(fail_receive=True)
        self.assertEqual(journals,[])

    def test_initramfs_failure_exports_both_pools_without_guid_handoff(self):
        events,journals=self.exercise(fail_refresh=True)
        self.assertEqual(events,['Restore bpool','Restore rpool'])
        self.assertEqual(journals,[])

    def test_second_guid_failure_cleans_remaining_temporary_pool(self):
        self.exercise(fail_guid=True)

    def test_unexpected_restored_dataset_aborts_before_guid_handoff(self):
        events,journals=self.exercise(fail_inventory=True)
        self.assertEqual(events,['Restore bpool'])
        self.assertEqual(journals,[])


class EphemeralBackupTests(unittest.TestCase):
    def test_conflicting_flags_fail_before_discovery(self):
        from types import SimpleNamespace
        for option in ('incremental','stack','snapshot_name','snapshot_index'):
            with self.subTest(option=option), patch.object(z,'discover') as discover:
                with self.assertRaisesRegex(z.Error,'--ephemeral cannot'):
                    z.backup(SimpleNamespace(ephemeral=True,**{option:True}))
                discover.assert_not_called()

    def test_partial_creation_cleanup_only_deletes_owned_snapshot(self):
        completed=[]
        with patch.object(z,'run',side_effect=['',z.Error('exists')]), patch('sys.stderr',new_callable=io.StringIO):
            with self.assertRaises(z.Error):
                z.snapshot_source_pools([{'name':'bpool'},{'name':'rpool'}],'temporary',completed)
        self.assertEqual(completed,['bpool@temporary'])
        with patch.object(z,'run') as run:
            z.remove_ephemeral_snapshots(completed)
        run.assert_called_once_with('zfs','destroy','-r','bpool@temporary')
        self.assertEqual(completed,[])

    def test_cleanup_failure_attempts_remaining_pools_and_reports(self):
        completed=['bpool@temporary','rpool@temporary']
        with patch.object(z,'run',side_effect=[z.Error('busy'),'']) as run:
            with self.assertRaisesRegex(z.Error,'bpool@temporary: busy'):
                z.remove_ephemeral_snapshots(completed)
        self.assertEqual(run.call_count,2)
        self.assertEqual(completed,['bpool@temporary'])

    def test_ephemeral_backup_cannot_be_incremental_base(self):
        m=fixture();old=copy.deepcopy(m);old['ephemeral']=True
        with patch.object(z,'catalog',return_value=[(Path('/backup'),old)]), patch.object(z,'run') as run:
            self.assertIsNone(z.incremental_parent('/repo',m))
        run.assert_not_called()

    def test_incremental_restore_rejected_before_destination_work(self):
        from types import SimpleNamespace
        m=fixture();m['ephemeral']=True
        with patch.object(z,'select_backup',return_value=Path('/backup')), \
             patch.object(z,'verify_chain',return_value=[(Path('/backup'),m)]), \
             patch.object(z,'restore_compression') as compression:
            with self.assertRaisesRegex(z.Error,'full restore only'):
                z.restore_from_storage(SimpleNamespace(incremental=True),'/repo')
        compression.assert_not_called()


class UbuntuSnapshotTests(unittest.TestCase):
    def test_each_pool_uses_a_separate_recursive_snapshot_command(self):
        with patch.object(z,'run') as run:
            z.snapshot_source_pools([{'name':'bpool'},{'name':'rpool'}],'point')
        self.assertEqual([c.args for c in run.call_args_list],[
            ('zfs','snapshot','-r','bpool@point'),
            ('zfs','snapshot','-r','rpool@point')])

    def test_snapshot_failure_reports_retained_pool_and_propagates(self):
        with patch.object(z,'run',side_effect=['',z.Error('snapshot failed')]) as run, \
             patch('sys.stderr',new_callable=io.StringIO) as output:
            with self.assertRaisesRegex(z.Error,'snapshot failed'):
                z.snapshot_source_pools([{'name':'bpool'},{'name':'rpool'}],'point')
        self.assertIn('created and retained: bpool@point',output.getvalue())
        self.assertNotIn('rpool@point',output.getvalue())
        self.assertEqual(run.call_count,2)

    def test_source_groups_merge_both_pools(self):
        with patch.object(z,'run',side_effect=['rpool@one\t10\t1\nrpool@two\t20\t2',
                                             'bpool@one\t11\t3']):
            groups=z.snapshot_groups(['rpool','bpool'])
        self.assertEqual([g['name'] for g in groups],['one','two'])
        self.assertEqual(groups[0]['snapshots'],[('rpool@one','1'),('bpool@one','3')])

    def test_source_scope_discovers_boot_pool_on_same_disk(self):
        with patch.object(z,'run',return_value=json.dumps({'filesystems':[{'fstype':'zfs','source':'bpool/BOOT/ubuntu'}]})), \
             patch.object(z,'pool_leaves',side_effect=[['/dev/sdj4'],['/dev/sdj2']]), \
             patch.object(z,'disk_for',return_value='/dev/sdj'):
            self.assertEqual(z.source_snapshot_pools('rpool'),['rpool','bpool'])

    def test_source_scope_rejects_boot_pool_on_another_disk(self):
        with patch.object(z,'run',return_value=json.dumps({'filesystems':[{'fstype':'zfs','source':'other/BOOT'}]})), \
             patch.object(z,'pool_leaves',side_effect=[['/dev/sdj4'],['/dev/sdk2']]), \
             patch.object(z,'disk_for',side_effect=['/dev/sdj','/dev/sdk']):
            with self.assertRaises(z.Error):z.source_snapshot_pools('rpool')


class RestoreEstimateTests(unittest.TestCase):
    def setUp(self):
        inventory=patch.object(z,'native_dataset_names',side_effect=lambda pool,remote=None:z.native_expected_names(pool))
        inventory.start();self.addCleanup(inventory.stop)

    def test_unattended_defaults_to_latest_and_honors_explicit_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            repository=Path(tmp)
            # Create out of order to check catalog chronology, not creation order.
            for day in (16,14,15):
                point=repository/f'backup-202609{day}-183000';point.mkdir()
                m=native_fixture();m['snapshot']=f'system-backup-202609{day}-183000'
                (point/'manifest.json').write_text(json.dumps(m))
                (point/'SHA256SUMS').write_text('')
            with patch.object(z,'select_host_repository',return_value=repository), \
                 patch('builtins.input',side_effect=AssertionError('Unattended restore prompted')), \
                 patch('sys.stdout',new_callable=io.StringIO):
                self.assertEqual(z.select_backup(repository,unattended=True),
                                 repository/'backup-20260916-183000')
                self.assertEqual(z.select_backup(repository,unattended=True,snapshot_index=2),
                                 repository/'backup-20260915-183000')
                self.assertEqual(z.select_backup(repository,unattended=True,snapshot='system-backup-20260914-183000'),
                                 repository/'backup-20260914-183000')
                self.assertIsNone(z.select_backup(repository,unattended=True,listing=True))
                with self.assertRaises(z.Error):
                    z.select_backup(repository,unattended=True,snapshot_index=4)
                with self.assertRaises(z.Error):
                    z.select_backup(repository,unattended=True,snapshot='missing')

    def test_sizes_shown_before_selection_use_full_estimate(self):
        m=native_fixture();m['backup_type']='incremental'
        m['pools'][0]['stream_bytes']=1024
        entries=[(Path('/repo/a'),m),(Path('/repo/b'),copy.deepcopy(m))]
        with patch.object(z,'select_host_repository',return_value=Path('/repo')), \
             patch.object(z,'catalog',return_value=entries), \
             patch.object(z,'estimate_native_send',return_value=6*z.GIB) as estimate, \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            def choose(prompt):
                self.assertEqual(estimate.call_count,2)
                self.assertEqual(output.getvalue().count('ZFS restore: ~6.000 GiB'),2)
                return '1'
            with patch('builtins.input',side_effect=choose):
                self.assertEqual(z.select_backup('/repo',show_sizes=True),Path('/repo/a'))
        self.assertEqual(m['pools'][0]['stream_bytes'],1024)

    def test_listing_reports_unavailable_estimate_without_prompt(self):
        m=native_fixture()
        with patch.object(z,'select_host_repository',return_value=Path('/repo')), \
             patch.object(z,'catalog',return_value=[(Path('/repo/a'),m)]), \
             patch.object(z,'estimate_native_send',side_effect=z.Error('missing snapshot')), \
             patch('builtins.input') as prompt,patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertIsNone(z.select_backup('/repo',listing=True,show_sizes=True))
            prompt.assert_not_called()
            self.assertIn('size unavailable: missing snapshot',output.getvalue())

    def test_estimates_full_native_stream_with_matching_raw_flags(self):
        pool=native_fixture()['pools'][0]
        pool['encrypted']=True
        pool['estimated_send_bytes']=100
        with patch.object(z,'run',return_value='full\tpool@snapshot\t5000\nsize\t6000\n') as run:
            self.assertEqual(z.estimate_native_send(pool,'selected'),6000)
        run.assert_called_once_with('zfs','send','-n','-P','-R','-b','-w',pool['native_dataset']+'@selected',combined=True)

    def test_invalid_estimate_aborts(self):
        with patch.object(z,'run',return_value='unparseable'):
            with self.assertRaises(z.Error):z.estimate_native_send(native_fixture()['pools'][0],'selected')


class RemoteCommandTests(unittest.TestCase):
    def setUp(self):
        inventory=patch.object(z,'native_dataset_names',side_effect=lambda pool,remote=None:z.native_expected_names(pool))
        inventory.start();self.addCleanup(inventory.stop)

    def test_ssh_command_quotes_arguments_and_preserves_host_checks(self):
        remote=z.RemoteRepository('backup@server',port=2222,executor='/opt/backup helper')
        argv=remote.command('zfs','set','example:note=$(touch /tmp/no); x','tank/data')
        self.assertEqual(z.shlex.split(argv[-1]),['env','LC_ALL=C','/opt/backup helper','zfs','set',
                                                'example:note=$(touch /tmp/no); x','tank/data'])
        self.assertIn('StrictHostKeyChecking=yes',argv)
        self.assertIn('BatchMode=yes',argv)
        self.assertIn('2222',argv)
        self.assertNotIn('sudo',argv)
        self.assertNotIn('/opt/backup helper',z.shlex.split(remote.command('python3','-c','pass')[-1]))

    def test_rejects_ssh_host_option_or_shell_injection(self):
        for host in ('-oProxyCommand=bad','user@host;whoami','host\nother','user name@host',''):
            with self.subTest(host=host),self.assertRaises(z.Error):z.RemoteRepository(host)

    def test_remote_native_send_wraps_only_sender(self):
        remote=z.RemoteRepository('server');remote.dataset='store'
        pool=native_fixture()['pools'][0]
        sender=z.native_send(pool,'selected',remote=remote)
        self.assertEqual(sender[0],'ssh')
        self.assertEqual(z.shlex.split(sender[-1])[2:],z.native_send(pool,'selected'))
        self.assertEqual(z.native_receive('local/dataset')[0],'zfs')

    def test_remote_estimate_uses_remote_full_send(self):
        remote=z.RemoteRepository('server');remote.dataset='store'
        pool=native_fixture()['pools'][0];pool['encrypted']=True
        with patch.object(remote,'run',return_value='size\t4096\n') as run:
            self.assertEqual(z.estimate_native_send(pool,'selected',remote=remote),4096)
        run.assert_called_once_with('zfs','send','-n','-P','-R','-b','-w',pool['native_dataset']+'@selected',combined=True)

    def test_remote_sender_rejects_dataset_outside_repository(self):
        remote=z.RemoteRepository('server');remote.dataset='other'
        with self.assertRaises(z.Error):z.native_send(native_fixture()['pools'][0],'selected',remote=remote)

    def test_cli_remote_options_and_local_conflicts(self):
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'backup') as backup:
            self.assertEqual(z.main(['backup','--remote','user@server','--pool','tank','--remote-executor','/usr/local/bin/priv-zfs']),0)
            self.assertEqual(backup.call_args.args[0].pool,'tank')
            backup.reset_mock()
            with patch('sys.stderr',new_callable=io.StringIO):
                self.assertEqual(z.main(['backup','--pool','tank']),1)
                self.assertEqual(z.main(['backup','--remote','server','--destination','/dev/sdb']),1)
            backup.assert_not_called()


class RemotePoolSelectionTests(unittest.TestCase):
    def test_forced_incremental_does_not_create_remote_repository(self):
        remote,read=self.remote(exists=False);remote.pool='tank'
        with patch.object(remote,'run',side_effect=read) as run,patch.object(remote,'files') as files:
            with self.assertRaisesRegex(z.Error,'--incremental requires an existing remote backup repository'):
                remote.select(write=True,require_existing=True)
        self.assertFalse(any(c.args[:2] in (('zfs','create'),('zfs','set')) for c in run.call_args_list))
        files.assert_not_called()

    def test_remote_storage_propagates_forced_incremental_policy(self):
        args=z.argparse.Namespace(remote='server',pool='tank',incremental=True)
        with patch.object(z,'commands'),patch.object(z.RemoteRepository,'select',side_effect=z.Error('stop')) as select:
            with self.assertRaisesRegex(z.Error,'stop'):
                with z.remote_storage(args,write=True):pass
        select.assert_called_once_with(True,forbidden_guids=(),require_existing=True)

    def remote(self,exists=True):
        remote=z.RemoteRepository('server')
        def read(*args,**kwargs):
            if args[:2]==('zpool','list'):return 'tank\t107374182400\t53687091200\tONLINE\t999\nother\t214748364800\t107374182400\tONLINE\t888\n'
            if args[:2]==('zfs','list'):
                return 'tank\nother\n'+('tank/linux_os_backup_repository\nother/linux_os_backup_repository\n' if exists else '')
            if args[:2]==('zfs','get'):
                return {'encryption':'off','mounted':'yes','mountpoint':'/remote/'+args[-1].split('/')[0],
                        'org.linux-os-backup:repository':'on'}[args[-2]]
            if args[0]=='findmnt':return json.dumps({'filesystems':[{'fstype':'zfs','source':remote.dataset,'target':'/remote/'+remote.pool}]})
            if args[:2]==('id','-u'):return '1000'
            return ''
        return remote,read

    def test_selects_numbered_pool_with_capacity_display(self):
        remote,read=self.remote()
        with patch.object(remote,'run',side_effect=read),patch.object(remote,'files'), \
             patch('builtins.input',return_value='2'),patch('sys.stdout',new_callable=io.StringIO) as output:
            remote.select()
        self.assertEqual(remote.pool,'other');self.assertEqual(remote.guid,'888')
        self.assertIn('free 100.0 GiB',output.getvalue())

    def test_creates_only_managed_child_and_grants_ssh_user_metadata_access(self):
        remote,read=self.remote(exists=False);remote.pool='tank';remote.executor='/helper'
        with patch.object(remote,'run',side_effect=read) as run,patch.object(remote,'files'),patch('builtins.input') as prompt:
            remote.select(write=True)
        prompt.assert_not_called()
        creates=[c.args for c in run.call_args_list if c.args[:2]==('zfs','create')]
        self.assertEqual(len(creates),1);self.assertEqual(creates[0][-1],'tank/linux_os_backup_repository')
        self.assertFalse(any(c.args[:2] in (('zpool','create'),('zpool','destroy'),('zpool','export')) for c in run.call_args_list))
        self.assertTrue(any(c.args[0]=='setfacl' and 'u:1000:rwx' in c.args for c in run.call_args_list))

    def test_restore_missing_repository_never_creates_it(self):
        remote,read=self.remote(exists=False);remote.pool='tank'
        with patch.object(remote,'run',side_effect=read) as run:
            with self.assertRaisesRegex(z.Error,'no remote backup repository'):remote.select()
        self.assertFalse(any(c.args[:2]==('zfs','create') for c in run.call_args_list))

    def test_source_pool_identity_rejected_before_repository_creation(self):
        remote,read=self.remote(exists=False);remote.pool='tank'
        with patch.object(remote,'run',side_effect=read) as run:
            with self.assertRaisesRegex(z.Error,'source OS pool'):remote.select(write=True,forbidden_guids={'999'})
        self.assertEqual(run.call_count,1)

    def test_unmanaged_repository_rejected(self):
        remote,read=self.remote();remote.pool='tank'
        def unmanaged(*args,**kwargs):
            if 'org.linux-os-backup:repository' in args:return '-'
            return read(*args,**kwargs)
        with patch.object(remote,'run',side_effect=unmanaged):
            with self.assertRaisesRegex(z.Error,'not managed'):remote.select(write=True)

    def test_unattended_remote_ambiguity_and_missing_repository_never_prompt_or_create(self):
        for exists,pool in ((True,None),(False,None),(False,'tank')):
            remote,read=self.remote(exists=exists);remote.pool=pool
            with patch.object(remote,'run',side_effect=read) as run, \
                 patch('builtins.input',side_effect=AssertionError('prompt')),self.assertRaises(z.Error):
                remote.select(write=True,unattended=True)
            self.assertFalse(any(c.args[:2] in (('zfs','create'),('zpool','create')) for c in run.call_args_list))

    def test_unattended_remote_never_guesses_a_pool_when_selection_would_prompt(self):
        remote,read=self.remote()
        with patch.object(remote,'run',side_effect=read),patch.object(remote,'files'), \
             patch('builtins.input',side_effect=AssertionError('prompt')), \
             self.assertRaisesRegex(z.Error,'specify --pool'):
            remote.select(write=True,forbidden_guids={'999'},unattended=True)
        self.assertIsNone(remote.pool)

    def test_unattended_remote_explicit_pool_resolves_ambiguity(self):
        remote,read=self.remote();remote.pool='tank'
        with patch.object(remote,'run',side_effect=read),patch.object(remote,'files'), \
             patch('builtins.input',side_effect=AssertionError('prompt')):
            remote.select(write=True,unattended=True)
        self.assertEqual(remote.pool,'tank')


class LocalRemoteFiles(z.RemoteRepository):
    """Execute the remote Python helper locally to exercise its actual protocol."""
    def command(self,*args):
        if args[0]!='python3':raise AssertionError('Unexpected non-metadata command: '+repr(args))
        return [sys.executable,*args[1:]]


class RemoteMetadataTests(unittest.TestCase):
    def repository(self,path):
        remote=LocalRemoteFiles('server');remote.mountpoint=str(path);remote.dataset='store';remote.guid='999'
        return remote

    def point(self,cache):
        point=Path(cache)/'hosts/proxmox2/backup-20260914-183000';point.mkdir(parents=True)
        (point/'disk').mkdir();(point/'efi').mkdir()
        (point/'manifest.json').write_text(json.dumps(native_fixture()))
        for name in ('disk/gpt.bin','disk/first-megabyte.bin','efi/esp-4.tar.zst','efi/esp-7.tar.zst'):
            (point/name).write_bytes(b'artifact '+name.encode())
        sums=''.join(z.digest(p)+'  '+str(p.relative_to(point))+'\n' for p in sorted(point.rglob('*')) if p.is_file())
        (point/'SHA256SUMS').write_text(sums)
        return point

    def test_publish_index_download_and_verify_selected_metadata(self):
        with tempfile.TemporaryDirectory() as server,tempfile.TemporaryDirectory() as cache,tempfile.TemporaryDirectory() as restored:
            remote=self.repository(server);remote.cache=Path(cache).resolve();point=self.point(cache)
            with remote.lock():remote.publish(point)
            self.assertTrue((Path(server)/point.relative_to(cache)/'SHA256SUMS').is_file())
            with remote.lock():
                remote.index(restored)
                selected=Path(restored)/point.relative_to(cache)
                self.assertFalse((selected/'efi').exists())
                remote.hydrate(selected)
                self.assertEqual((selected/'efi/esp-4.tar.zst').read_bytes(),(point/'efi/esp-4.tar.zst').read_bytes())
                with patch.object(remote,'run',return_value='123'),patch.object(z,'inspect_tar'):
                    m=z.verify_backup(selected,read_native=False,remote=remote)
                self.assertEqual(m['snapshot'],native_fixture()['snapshot'])

    def test_corrupt_upload_never_publishes_complete_point(self):
        with tempfile.TemporaryDirectory() as server,tempfile.TemporaryDirectory() as cache:
            remote=self.repository(server);remote.cache=Path(cache).resolve();point=self.point(cache)
            (point/'disk/gpt.bin').write_bytes(b'corrupt')
            with self.assertRaisesRegex(z.Error,'checksum mismatch'):remote.publish(point)
            self.assertFalse((Path(server)/point.relative_to(cache)).exists())
            self.assertEqual(json.loads(remote.files('index')),[])

    def test_remote_helper_rejects_path_traversal_and_symlinks(self):
        with tempfile.TemporaryDirectory() as server:
            remote=self.repository(server)
            (Path(server)/'escape').symlink_to('/tmp')
            for relative in ('../escape','/etc/passwd','escape/file'):
                with self.subTest(relative=relative),self.assertRaises(z.Error):
                    remote.files('get',relative=relative)

    def test_remote_lock_excludes_concurrent_operation_and_releases_on_error(self):
        with tempfile.TemporaryDirectory() as server:
            first=self.repository(server);second=self.repository(server)
            with self.assertRaisesRegex(z.Error,'test failure'):
                with first.lock():
                    with self.assertRaises(z.Error):
                        with second.lock():self.fail('Concurrent lock accepted')
                    raise z.Error('test failure')
            with second.lock():second.check_lock()

    def test_index_rejects_remote_manifest_storage_identity(self):
        remote=self.repository('/unused')
        m=native_fixture();m['storage_pool_guid']='111'
        index=[dict(host='proxmox2',point='backup-20260914-183000',manifest=json.dumps(m),sums='')]
        with tempfile.TemporaryDirectory() as cache,patch.object(remote,'files',return_value=json.dumps(index)):
            with self.assertRaisesRegex(z.Error,'different storage repository'):remote.index(cache)


class RemoteReplicationTests(unittest.TestCase):
    def test_lost_lock_stops_commands_before_another_remote_operation(self):
        remote=z.RemoteRepository('server')
        remote.lock_process=unittest.mock.Mock()
        remote.lock_process.poll.return_value=255
        with patch.object(z,'run') as run:
            with self.assertRaisesRegex(z.Error,'lock connection was lost'):
                remote.run('zpool','sync','tank')
        run.assert_not_called()

    def test_remote_verify_routes_selected_point_to_full_verification(self):
        from argparse import Namespace
        remote=z.RemoteRepository('server');base=Path('/cache');point=base/'hosts/nas/point'
        args=Namespace(remote='server',snapshot=None,list_snapshots=False,host='nas')
        with patch.object(z,'commands'), \
             patch.object(z,'remote_storage',return_value=z.contextlib.nullcontext((base,remote))), \
             patch.object(z,'select_backup',return_value=point), \
             patch.object(z,'verify_chain',return_value=[(point,native_fixture())]) as verify, \
             patch.object(z,'choose_backup_storage') as local:
            z.verify(args)
        verify.assert_called_once_with(point,remote=remote)
        local.assert_not_called()

    def test_incremental_base_verification_remote_source_guid_checks_local(self):
        m=native_fixture();remote=z.RemoteRepository('server')
        with patch.object(z,'catalog',return_value=[(Path('/repo/base'),m)]), \
             patch.object(z,'verify_chain') as verify,patch.object(z,'run',return_value='123') as run:
            z.incremental_parent('/repo',copy.deepcopy(m),native_only=True,remote=remote)
        verify.assert_called_once_with(Path('/repo/base'),read_native=False,remote=remote)
        self.assertTrue(all(c.args[0]=='zfs' for c in run.call_args_list))

    def test_pending_remote_generation_blocks_incremental(self):
        remote=z.RemoteRepository('server')
        with patch.object(remote,'run',return_value='incomplete'),patch.object(z,'run') as local:
            with self.assertRaisesRegex(z.Error,'incomplete backup'):
                z.assert_native_tip(native_fixture()['pools'][0],'base',remote=remote)
        local.assert_not_called()

    def test_remote_backup_routes_receive_hold_sync_and_publish(self):
        self.backup_run()

    def test_remote_receive_failure_keeps_pending_and_never_publishes(self):
        self.backup_run(fail=True)

    def test_second_current_only_receive_failure_keeps_generation_pending(self):
        self.backup_run(fail=True,late=True)

    def test_history_opt_in_routes_recursive_stream_and_keeps_manifest_history(self):
        self.backup_run(policy='history')

    def test_stacked_incremental_estimates_and_transfers_new_dataset_history(self):
        self.backup_run(policy='history',incremental=True)

    def test_default_incremental_on_history_base_sends_only_current_snapshots(self):
        self.backup_run(incremental=True)

    def test_reseeded_pool_uses_fresh_namespace_and_full_streams(self):
        self.backup_run(incremental=True,reseed=True)

    def test_reseeded_stacked_pool_uses_full_recursive_stream(self):
        self.backup_run(incremental=True,reseed=True,policy='history')

    def test_older_common_snapshot_stops_before_incremental_transfer(self):
        self.backup_run(incremental=True,blocked='newer checkpoint')

    def test_incomplete_backup_leaves_prior_generation_untouched(self):
        self.backup_run(incremental=True,blocked='incomplete backup')

    def test_incomplete_receive_stops_before_incremental_transfer(self):
        self.backup_run(incremental=True,blocked='Incomplete native receive')

    def test_forced_incremental_uses_matching_base(self):
        self.backup_run(incremental=True,forced=True)

    def test_forced_incremental_missing_base_never_sends_or_publishes(self):
        self.backup_run(forced=True,blocked='requires a usable matching base')

    def test_forced_incremental_rejects_full_pool_reseed(self):
        self.backup_run(incremental=True,reseed=True,forced=True,blocked='requires a usable matching base')

    def test_backup_compression_reaches_every_remote_receive(self):
        self.backup_run(compression='zstd-3')

    def test_backup_compression_applies_to_boot_pool_copy(self):
        self.backup_run(compression='zstd-3',ubuntu=True)

    def test_stacked_backup_compression_applies_to_boot_pool_copy(self):
        self.backup_run(compression='zstd-3',ubuntu=True,policy='history')

    def test_ephemeral_backup_cleans_source_after_publication(self):
        self.backup_run(ephemeral=True)

    def test_ephemeral_backup_cleans_source_after_transfer_failure(self):
        self.backup_run(ephemeral=True,fail=True)

    def backup_run(self,fail=False,late=False,policy=None,incremental=False,reseed=False,blocked=None,forced=False,compression=None,ubuntu=False,ephemeral=False):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);repository=root/'cache';repository.mkdir()
            source=root/'source';source.write_bytes(b'x'*z.MIB)
            m=ubuntu_fixture() if ubuntu else native_fixture();m['disk']['device']=str(source)
            if ubuntu:
                boot=m['pools'][0]
                boot['properties']['feature@zstd_compress']={'value':'disabled'}
                boot['datasets']['bpool']['compression']={'value':'lz4','source':'local'}
                boot['datasets']['bpool/BOOT/ubuntu_1opcom']['compression']={'value':'gzip-6','source':'local'}
            previous=None
            if incremental:
                old=copy.deepcopy(m);old['snapshot_history']='history'
                old_path=repository/'base';old_path.mkdir()
                (old_path/'manifest.json').write_text(json.dumps(old))
                previous=(old_path,old)
                old['pools'][0]['datasets']['tank/retired']={}
                old['pools'][0]['datasets']['tank/retired@'+old['snapshot']]={'guid':{'value':'123'}}
                m['pools'][0]['datasets']['tank/new']={}
                m['pools'][0]['datasets']['tank/new@seed']={'guid':{'value':'123'},'createtxg':{'value':'1'}}
                if reseed:m['pools'][0]['replication_type']='full'
            remote=z.RemoteRepository('server');remote.dataset='store';remote.guid='999';remote.cache=repository
            events=[];estimates=[];local_events=[]
            def remote_run(*args,**kwargs):
                events.append(args)
                if args[:2]==('zfs','get') and 'guid' in args:return '123'
                if 'feature@zstd_compress' in args:return 'enabled'
                if args[:2]==('zfs','list') and args[-2]=='name,type,guid,createtxg,referenced,origin':
                    root=args[-1]
                    pool=next(p for p in m['pools'] if p['native_dataset']==root)
                    old=incremental and pool.get('replication_type')!='full'
                    rows=f'{root}\tfilesystem\t1\t1\t0\t-\n'
                    if old:rows+=f'{root}@base\tsnapshot\t10\t2\t{9*z.GIB}\t-\n'
                    sent=any(z.shlex.split(entry[1][-1])[-1]==root for entry in transfers)
                    if sent:rows+=f'{root}@tip\tsnapshot\t11\t3\t{9*z.GIB+500 if old else 500}\t-\n'
                    return rows
                if args[:2]==('zfs','get') and args[-2].startswith('written@'):
                    return '500'
                if args[:2]==('zfs','list') and args[-2].endswith(',compressratio'):
                    self.assertIn(('zpool','sync','store'),events)
                    self.assertIn(('publish',),events)
                    return f'{args[-1]}\t{z.GIB}\t0\t{2*z.GIB}\t2.35\n'
                return ''
            def local_run(*args,**kwargs):
                local_events.append(args)
                if args[:3]==('zfs','send','-n'):
                    estimates.append(args[-1]);return 'size\t1000\n'
                if args[0]=='sgdisk' and args[1].startswith('--backup='):
                    Path(args[1].split('=',1)[1]).write_bytes(b'gpt')
                return ''
            def properties(program,name,recursive=False):
                datasets=copy.deepcopy(next(p['datasets'] for p in m['pools'] if p['name']==name))
                for dataset in list(datasets):
                    if '@' not in dataset:datasets[dataset+'@'+m['snapshot']]={'guid':{'value':'123'},'createtxg':{'value':'2'}}
                return datasets
            original_read=Path.read_text;original_is_file=Path.is_file
            def read(path,*a,**kw):
                return '' if str(path)=='/etc/fstab' else original_read(path,*a,**kw)
            def is_file(path):
                return True if str(path).endswith(('.efi.signed',)) else original_is_file(path)
            def publish(point):
                self.assertTrue((point/'SHA256SUMS').is_file())
                saved=json.loads((point/'manifest.json').read_text())
                self.assertEqual(saved['version'],5)
                self.assertEqual(saved.get('ephemeral',False),ephemeral)
                if ubuntu:
                    boot=saved['pools'][0]
                    self.assertEqual(boot['properties']['feature@zstd_compress']['value'],'disabled')
                    self.assertEqual(boot['datasets']['bpool']['compression']['value'],'lz4')
                    self.assertEqual(boot['datasets']['bpool/BOOT/ubuntu_1opcom']['compression']['value'],'gzip-6')
                self.assertRegex(saved['snapshot'],r'^system-backup-\d{8}-\d{6}$')
                self.assertEqual(point.name,'backup-'+z.snapshot_stamp(saved['snapshot']))
                self.assertEqual(saved['snapshot_history'],policy or 'current')
                if policy!='history':
                    self.assertTrue(all('@' not in n or n.endswith('@'+saved['snapshot'])
                                        for p in saved['pools'] for n in p['datasets']))
                else:
                    self.assertTrue(any('@' in n and not n.endswith('@'+saved['snapshot'])
                                        for p in saved['pools'] for n in p['datasets']))
                self.assertTrue(any(e[:2]==('zfs','hold') for e in events))
                self.assertFalse(any(e[:2]==('zfs','inherit') for e in events))
                events.append(('publish',))
            args=Namespace(destination=None,remote='server',full=not incremental and not forced and not ephemeral,
                           incremental=forced,incremental_from=None,stack=policy=='history',compression=compression,ephemeral=ephemeral)
            transfers=[]
            def transfer_stream(*args,**kwargs):
                transfers.append(args)
                if fail and (not late or len(transfers)==2):raise z.Error('remote failed')
                if 'on_bytes' in kwargs:kwargs['on_bytes'](1234)
                return 1234
            progress_output=io.StringIO();completion_output=io.StringIO()
            with patch.object(z,'commands'),patch.object(z,'discover',return_value=m), \
                 patch.object(z,'incremental_parent',return_value=previous) as select_parent, \
                 patch.object(z,'assert_native_tip',side_effect=z.Error(blocked) if blocked else None), \
                 patch.object(z,'estimate_native_send',return_value=1000), \
                 patch.object(z,'native_dataset_names',side_effect=lambda p,remote=None:z.native_expected_names(p)), \
                 patch.object(z.socket,'gethostname',return_value='proxmox2'), \
                 patch.object(z,'backup_storage',return_value=z.contextlib.nullcontext((repository,remote))), \
                 patch.object(z,'props',side_effect=properties),patch.object(z,'run',side_effect=local_run), \
                 patch.object(remote,'run',side_effect=remote_run),patch.object(remote,'available',return_value=100*z.GIB), \
                 patch.object(remote,'publish',side_effect=publish) as publication, \
                 patch.object(z.shutil,'disk_usage',return_value=Namespace(free=100*z.GIB)), \
                 patch.object(z,'pack_esp',side_effect=lambda device,path:path.write_bytes(b'efi')), \
                 patch.object(Path,'read_text',read),patch.object(Path,'is_file',is_file), \
                 patch.object(z,'pipe_transfer',side_effect=transfer_stream) as transfer, \
                 patch.multiple(sys,stderr=progress_output,stdout=completion_output):
                if blocked:
                    with self.assertRaisesRegex(z.Error,blocked):z.backup(args)
                elif fail:
                    with self.assertRaisesRegex(z.Error,'remote failed'):z.backup(args)
                else:
                    transferred=z.backup(args)
                    self.assertEqual(transferred.stream,1234*len(transfers))
                    self.assertEqual(transferred.uncompressed,1234*len(transfers))
                    self.assertEqual(transferred.compressed,500*len(m['pools']))
                    self.assertEqual(transferred.compression,compression or 'no override')
            if ephemeral:
                self.assertTrue(select_parent.call_args.args[3])
                self.assertEqual([e for e in local_events if e[:2]==('zfs','destroy')],
                                 [('zfs','destroy','-r',p['name']+'@'+m['snapshot']) for p in m['pools']])
                self.assertNotIn('Source snapshot retained:',completion_output.getvalue())
            output=progress_output.getvalue()
            self.assertNotIn('skipped for boot compatibility',completion_output.getvalue())
            ratio_reads=[e[-1] for e in events if e[:2]==('zfs','list') and e[-2].endswith(',compressratio')]
            self.assertEqual(ratio_reads,[] if fail or blocked else [p['native_dataset'] for p in m['pools']])
            if not fail and not blocked:
                for pool in m['pools']:
                    self.assertIn(pool['name']+': compressed 1.000 GiB, uncompressed 2.000 GiB | ratio 2.35x'
                                  +' | compression: '+(compression or 'no override'),completion_output.getvalue())
                    headers=[line for line in completion_output.getvalue().splitlines() if ' Sending '+pool['name']+'@' in line]
                    self.assertEqual(len(headers),1)
                    self.assertRegex(headers[0],r'^\[\d{4}-\d{2}-\d{2} .*\] Sending ')
                    self.assertIn('compressed ',headers[0]);self.assertIn('uncompressed ',headers[0])
                self.assertNotIn('Selected snapshot',completion_output.getvalue())
                overall=next(line for line in completion_output.getvalue().splitlines() if line.startswith('  Overall:'))
                self.assertTrue(overall.endswith(' | compression: '+(compression or 'no override')))
            self.assertEqual(select_parent.call_args.kwargs.get('required',False),forced)
            self.assertNotIn('Replicate tank/',output)
            if blocked:
                if previous:self.assertEqual(m['native_root'],old['native_root'])
                transfer.assert_not_called();publication.assert_not_called()
                self.assertEqual(events,[]);self.assertEqual(estimates,[])
                self.assertFalse(list(repository.rglob('backup-*')))
                if previous:
                    self.assertEqual(json.loads((old_path/'manifest.json').read_text())['native_root'],old['native_root'])
                return
            if fail:
                self.assertNotIn(' | complete',output)
                self.assertEqual(output.count(' | failed/interrupted'),1)
            else:
                self.assertEqual(output.count(' | complete'),len(m['pools']))
                for pool in m['pools']:self.assertIn('Replicate '+pool['name']+':',output)
            sender,receiver,_,_=transfer.call_args.args
            self.assertEqual(sender[:2],['zfs','send']);self.assertEqual(receiver[0],'ssh')
            if policy=='history' and (not incremental or reseed):self.assertIn('-R',sender)
            else:self.assertIn('-p',sender);self.assertNotIn('-R',sender)
            if reseed:
                self.assertNotEqual(m['pools'][0]['native_dataset'],old['pools'][0]['native_dataset'])
                self.assertEqual(m['pools'][0]['native_dataset'],m['native_root']+'/tank-'+m['snapshot'])
                self.assertTrue(all('-i' not in entry[0] and '-I' not in entry[0] for entry in transfers))
                self.assertTrue(all(z.shlex.split(entry[1][-1])[-1].startswith(m['pools'][0]['native_dataset']) for entry in transfers))
            elif incremental:
                sent=[entry[0][-1] for entry in transfers]
                self.assertEqual(sent,estimates)
                self.assertEqual('tank/new@seed' in sent,policy=='history')
                existing=[entry[0] for entry in transfers if entry[0][-1].startswith('tank@')]
                self.assertEqual(len(existing),1)
                self.assertIn('-I' if policy=='history' else '-i',existing[0])
            if late:self.assertEqual(len(transfers),2)
            receive_args=z.shlex.split(receiver[-1])
            self.assertIn('receive',receive_args);self.assertNotIn('-F',receive_args)
            for entry in transfers:
                settings=[a for a in z.shlex.split(entry[1][-1]) if a.startswith('compression=')]
                self.assertEqual(settings,['compression='+compression] if compression else [])
                if compression:
                    self.assertNotIn('-c',entry[0]);self.assertNotIn('-w',entry[0])
            self.assertTrue(any(e[:2]==('zfs','set') and any('pending=' in a for a in e) for e in events))
            holds=[e for e in events if e[:2]==('zfs','hold')]
            self.assertTrue(all('-r' not in e and not any('/retired@' in a for a in e) for e in holds))
            if fail:
                publication.assert_not_called()
                self.assertFalse(any(e[:2]==('zfs','inherit') for e in events))
            else:
                self.assertLess(events.index(('publish',)),next(i for i,e in enumerate(events) if e[:2]==('zfs','inherit')))

    def test_remote_restore_dispatch_does_not_discover_local_backup_disk(self):
        from argparse import Namespace
        remote=z.RemoteRepository('server')
        with patch.object(z,'commands'),patch.object(z,'remote_storage',return_value=z.contextlib.nullcontext((Path('/stage'),remote))), \
             patch.object(z,'choose_backup_storage') as choose,patch.object(z,'restore_from_storage') as restore:
            args=Namespace(remote='server')
            z.restore(args)
        choose.assert_not_called();restore.assert_called_once_with(args,Path('/stage'),remote=remote)


class CurrentOnlyTests(unittest.TestCase):
    def setUp(self):
        inventory=patch.object(z,'native_dataset_names',side_effect=lambda pool,remote=None:z.native_expected_names(pool))
        inventory.start();self.addCleanup(inventory.stop)

    def test_source_snapshot_size_sums_selected_dataset_streams_without_history(self):
        groups=[dict(name='point',snapshots=[('tank@point','1'),('tank/vm@point','2')])]
        with patch.object(z,'run',side_effect=['size\t100\n','size\t200\n']) as run:
            z.estimate_snapshot_groups(groups)
        self.assertEqual(groups[0]['restore_bytes'],300)
        send=[c.args for c in run.call_args_list if c.args[:2]==('zfs','send')]
        self.assertEqual(send,[('zfs','send','-n','-P','tank@point'),
                               ('zfs','send','-n','-P','tank/vm@point')])

    def manifest(self):
        m=native_fixture();m['snapshot_history']='current'
        p=m['pools'][0]
        p['datasets']['tank/ROOT']={}
        p['datasets']['tank/vm']={'type':{'value':'volume'},'origin':{'value':'tank/template@old'}}
        p['datasets']['tank@old']={'used':{'value':str(100*z.GIB)}}
        return m,p

    def test_default_and_stack_are_per_invocation(self):
        from argparse import Namespace
        self.assertEqual(z.select_snapshot_history(Namespace()),'current')
        self.assertEqual(z.select_snapshot_history(Namespace(stack=False)),'current')
        self.assertEqual(z.select_snapshot_history(Namespace(stack=True)),'history')

    def test_full_sends_one_snapshot_per_dataset_parent_first_and_flattens_clone(self):
        m,p=self.manifest();streams=z.backup_streams(p,m)
        self.assertEqual([s['source'] for s in streams],['tank','tank/ROOT','tank/vm','tank/ROOT/ubuntu'])
        for stream in streams:
            self.assertEqual(stream['flags'],['-p'])
            self.assertEqual(stream['destination'],p['native_dataset']+stream['source'][len(p['name']):])
        volume=next(s for s in streams if s['source']=='tank/vm')
        self.assertTrue(volume['volume'])
        receive=z.native_receive(volume['destination'],volume=True)
        self.assertIn('volmode=none',receive);self.assertIn('readonly=on',receive)
        self.assertNotIn('canmount=off',receive);self.assertNotIn('-F',receive)

    def test_incremental_skips_intermediate_snapshots_and_full_sends_new_datasets(self):
        m,p=self.manifest();previous=copy.deepcopy(p)
        p['datasets']['tank/new']={}
        m.update(backup_type='incremental',base_snapshot='baremetal-20260914-183000')
        for stream in z.backup_streams(p,m,previous):
            if stream['source']=='tank/new':self.assertEqual(stream['flags'],['-p'])
            else:self.assertEqual(stream['flags'],['-p','-i',stream['source']+'@'+m['base_snapshot']])

    def test_stack_can_be_toggled_on_the_same_incremental_base(self):
        m,p=self.manifest();previous=copy.deepcopy(p)
        m.update(backup_type='incremental',base_snapshot='base')
        for stacked in (False,True,False):
            m['snapshot_history']='history' if stacked else 'current'
            for stream in z.backup_streams(p,m,previous):
                self.assertEqual(stream['flags'],['-p','-I' if stacked else '-i',stream['source']+'@base'])
                self.assertNotIn('-R',stream['flags'])

    def test_stack_new_dataset_seeds_oldest_then_sends_history_in_txg_order(self):
        m,p=self.manifest();previous=copy.deepcopy(p)
        m.update(snapshot_history='history',backup_type='incremental',base_snapshot='base')
        p['datasets'].update({'tank/new':{},
            'tank/new@z-old':{'createtxg':{'value':'2'}},
            'tank/new@a-middle':{'createtxg':{'value':'3'}},
            'tank/new@'+m['snapshot']:{'createtxg':{'value':'4'}},
            'tank/new@after-backup':{'createtxg':{'value':'5'}}})
        new=[s for s in z.backup_streams(p,m,previous) if s['source']=='tank/new']
        self.assertEqual(len(new),2)
        self.assertEqual(new[0]['snapshot'],'z-old')
        self.assertEqual(new[0]['flags'],['-p'])
        self.assertEqual(new[1]['snapshot'],m['snapshot'])
        self.assertEqual(new[1]['flags'],['-p','-I','tank/new@z-old'])
        self.assertEqual(new[0]['destination'],new[1]['destination'])

    def test_stack_new_dataset_with_only_checkpoint_needs_one_full_send(self):
        m,p=self.manifest();previous=copy.deepcopy(p)
        m.update(snapshot_history='history',backup_type='incremental',base_snapshot='base')
        p['datasets']['tank/new']={}
        p['datasets']['tank/new@'+m['snapshot']]={'createtxg':{'value':'4'}}
        new=[s for s in z.backup_streams(p,m,previous) if s['source']=='tank/new']
        self.assertEqual(len(new),1)
        self.assertEqual(new[0]['flags'],['-p'])

    def test_default_incremental_preserves_preexisting_encrypted_relationships(self):
        m,p=self.manifest()
        p['datasets']['tank/private']={'encryption':{'value':'aes-256-gcm'},
            'encryptionroot':{'value':'tank/parent'},'origin':{'value':'tank/other@old'}}
        previous=copy.deepcopy(p)
        m.update(backup_type='incremental',base_snapshot='base')
        stream=next(s for s in z.backup_streams(p,m,previous) if s['source']=='tank/private')
        self.assertEqual(stream['flags'],['-p','-w','-i','tank/private@base'])

    def test_history_opt_in_keeps_recursive_full_and_intermediate_incrementals(self):
        m,p=self.manifest();m['snapshot_history']='history'
        self.assertEqual(z.backup_streams(p,m)[0]['flags'],['-R'])
        m.update(backup_type='incremental',base_snapshot='base')
        streams=z.backup_streams(p,m,copy.deepcopy(p))
        self.assertEqual(len(streams),4)
        for stream in streams:
            self.assertEqual(stream['flags'],['-p','-I',stream['source']+'@base'])

    def test_encryption_root_uses_raw_without_unrelated_unencrypted_datasets(self):
        m,p=self.manifest();p['encrypted']=True
        p['datasets']['tank/private']={'encryption':{'value':'aes-256-gcm'},
                                       'encryptionroot':{'value':'tank/private'}}
        for stream in z.backup_streams(p,m):
            self.assertEqual('-w' in stream['flags'],stream['source']=='tank/private')

    def test_inherited_encryption_and_encrypted_clones_fail_with_history_remedy(self):
        for root,origin in [('tank/parent','-'),('tank/private','tank/other@old')]:
            m,p=self.manifest()
            p['datasets']['tank/private']={'encryption':{'value':'aes-256-gcm'},
                'encryptionroot':{'value':root},'origin':{'value':origin}}
            with self.assertRaisesRegex(z.Error,'--full --stack'):z.backup_streams(p,m)

    def test_restore_capacity_excludes_history_but_counts_live_clones_and_reservations(self):
        m,p=self.manifest()
        p['estimated_send_bytes']=p['stream_bytes']=2*z.GIB
        p['datasets']['tank']['referenced']={'value':str(z.GIB)}
        p['datasets']['tank/vm'].update(logicalreferenced={'value':str(3*z.GIB)},
                                       refreservation={'value':str(z.GIB)})
        self.assertEqual(z.current_dataset_bytes(p),4*z.GIB)
        plan=z.solve(m,20*z.GIB,512)
        self.assertEqual(plan['pools']['tank']['allocation_bytes'],5*z.GIB)
        del m['snapshot_history']
        with self.assertRaisesRegex(z.Error,'Target too small'):z.solve(m,20*z.GIB,512)

    def test_current_only_manifest_still_restores_via_full_native_replication(self):
        m,p=self.manifest();z.validate(m)
        self.assertEqual(z.native_send(p,m['snapshot']),['zfs','send','-R','-b',p['native_dataset']+'@'+m['snapshot']])
        m['snapshot_history']='invalid'
        with self.assertRaisesRegex(z.Error,'snapshot history policy'):z.validate(m)

    def test_cli_current_default_and_stack_for_full_and_incremental(self):
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'backup') as backup:
            for full in ([],['--full']):
                self.assertEqual(z.main(['backup',*full]),0)
                self.assertFalse(backup.call_args.args[0].stack)
                self.assertEqual(z.main(['backup',*full,'--stack']),0)
                self.assertTrue(backup.call_args.args[0].stack)


class SnapshotPrefixTests(unittest.TestCase):
    def test_manifest_accepts_new_prefix_and_legacy_generation_with_new_incremental(self):
        m=native_fixture();z.validate(m)
        old=m['snapshot'];m['snapshot']='system-backup-20260915-120000'
        m.update(backup_type='incremental',base_snapshot=old,parent='backup-20260914-183000',
                 parent_manifest_sha256='a'*64)
        z.validate(m)
        root=m['native_root'].replace('baremetal-','system-backup-')
        for pool in m['pools']:
            pool['native_dataset']=pool['native_dataset'].replace(m['native_root'],root)
        m['native_root']=root
        z.validate(m)

    def test_tip_uses_timestamp_instead_of_prefix_sort_order(self):
        p=native_fixture()['pools'][0]
        old='baremetal-20260917-120000';new='system-backup-20260916-120000'
        names='\n'.join(p['native_dataset']+'@'+n for n in (new,old))
        with patch.object(z,'run',side_effect=['-','-',names]):
            with self.assertRaisesRegex(z.Error,'newer snapshot'):z.assert_native_tip(p,new)
        with patch.object(z,'run',side_effect=['-','-',names]):
            z.assert_native_tip(p,old)

    def test_tip_accepts_first_new_prefix_checkpoint_after_legacy_base(self):
        p=native_fixture()['pools'][0];new='system-backup-20260916-120000'
        names='\n'.join(p['native_dataset']+'@'+n for n in ('baremetal-20260914-183000',new))
        with patch.object(z,'run',side_effect=['-','-',names]):z.assert_native_tip(p,new)

    def test_newer_external_child_snapshot_stops_incremental_backup(self):
        p=native_fixture()['pools'][0];native=p['native_dataset'];base='system-backup-20260916-120000'
        names='\n'.join((native+'@'+base,native+'/ROOT@'+base,native+'/ROOT@manual-checkpoint'))
        with patch.object(z,'run',side_effect=['-','-',names]):
            with self.assertRaisesRegex(z.Error,'automatic backup branches are unsupported'):z.assert_native_tip(p,base)

    def test_mixed_prefix_listing_and_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            for snapshot in ('baremetal-20260914-183000','system-backup-20260915-120000'):
                m=native_fixture();m['snapshot']=snapshot
                path=Path(tmp)/('backup-'+z.snapshot_stamp(snapshot));path.mkdir()
                (path/'manifest.json').write_text(json.dumps(m));(path/'SHA256SUMS').write_text('')
            with patch('sys.stdout',new_callable=io.StringIO) as output:
                selected=z.select_backup(tmp,snapshot='system-backup-20260915-120000')
            self.assertEqual(selected.name,'backup-20260915-120000')
            self.assertIn('baremetal-20260914-183000',output.getvalue())
            self.assertIn('system-backup-20260915-120000',output.getvalue())

    def test_timestamp_rejects_unrecognized_prefixes(self):
        self.assertEqual(z.snapshot_stamp('system-backup-20260915-120000'),'20260915-120000')
        with self.assertRaises(z.Error):z.snapshot_stamp('other-20260915-120000')


class DatasetChangeTests(unittest.TestCase):
    def test_version_five_requires_native_storage_and_retains_v4_compatibility(self):
        old=native_fixture();z.validate(old)
        new=copy.deepcopy(old);new['version']=5;z.validate(new)
        new['storage']='files'
        with self.assertRaisesRegex(z.Error,'requires native storage'):z.validate(new)

    def test_replacing_root_pool_child_does_not_reseed_boot_pool(self):
        old=ubuntu_fixture();current=copy.deepcopy(old)
        root=old['pools'][1];root['datasets']['rpool/vm']={'guid':{'value':'100'}}
        root['datasets']['rpool/vm@'+old['snapshot']]={'guid':{'value':'123'}}
        current['pools'][1]['datasets']['rpool/vm']={'guid':{'value':'200'}}
        def source_guid(*args,**kwargs):
            pool=next(p for p in old['pools'] if args[-1].startswith(p['name']))
            return z.val(pool['datasets'][args[-1]],'guid')
        with patch.object(z,'catalog',return_value=[(Path('/repo/base'),old)]), \
             patch.object(z,'verify_chain'),patch.object(z,'run',side_effect=source_guid):
            z.incremental_parent('/repo',current,native_only=True)
        self.assertNotIn('replication_type',current['pools'][0])
        self.assertEqual(current['pools'][1]['replication_type'],'full')

    def prior(self):
        old=native_fixture();p=old['pools'][0]
        for name in ('tank/data','tank/data/vm-101-disk-0','tank/data/vm-101-disk-1'):
            p['datasets'][name]={'guid':{'value':name[-1]+'100'}}
            p['datasets'][name+'@'+old['snapshot']]={'guid':{'value':'123'}}
        return old

    def choose(self,current,old,stored=None):
        p=old['pools'][0];native=p['native_dataset']
        if stored is None:
            stored={native,native+'/ROOT',native+'/ROOT/ubuntu',native+'/data',
                    native+'/data/vm-101-disk-0',native+'/data/vm-101-disk-1'}
        def run(*args,**kwargs):
            if args[:2]==('zfs','get'):return '123'
            if args[:2]==('zfs','list'):return '\n'.join(sorted(stored))
            raise AssertionError(args)
        with patch.object(z,'catalog',return_value=[(Path('/repo/base'),old)]), \
             patch.object(z,'verify_chain'),patch.object(z,'run',side_effect=run) as calls:
            z.incremental_parent('/repo',current,native_only=True)
        current.update(backup_type='incremental',base_snapshot=old['snapshot'],snapshot_history='current')
        return calls

    def test_vm_removal_and_addition_send_only_new_disks_full(self):
        old=self.prior();current=copy.deepcopy(old);p=current['pools'][0]
        p['datasets']={n:ps for n,ps in p['datasets'].items() if '/vm-101-' not in n}
        for name in ('tank/data/vm-107-disk-0','tank/data/vm-107-disk-1'):
            p['datasets'][name]={'type':{'value':'volume'}}
        calls=self.choose(current,old)
        self.assertNotIn('replication_type',p)
        self.assertFalse(any('/vm-101-' in c.args[-1] for c in calls.call_args_list if c.args[:2]==('zfs','get')))
        streams=z.backup_streams(p,current,old['pools'][0])
        self.assertEqual({s['source'] for s in streams if '-i' not in s['flags']},
                         {'tank/data/vm-107-disk-0','tank/data/vm-107-disk-1'})
        self.assertTrue(all('/vm-101-' not in s['destination'] for s in streams))

    def test_reused_removed_name_automatically_reseeds_pool_without_overwrite(self):
        old=self.prior();p=old['pools'][0];native=p['native_dataset']
        old['pools'][0]['datasets']={n:ps for n,ps in p['datasets'].items() if '/vm-101-' not in n}
        current=copy.deepcopy(old);current['pools'][0]['datasets']['tank/data/vm-101-disk-0']={}
        self.choose(current,old)
        self.assertEqual(current['pools'][0]['replication_type'],'full')
        streams=z.backup_streams(current['pools'][0],current,old['pools'][0])
        self.assertTrue(all('-i' not in s['flags'] and '-I' not in s['flags'] for s in streams))
        self.assertEqual(p['native_dataset'],native)

    def test_replaced_dataset_guid_or_promoted_clone_reseeds_only_affected_pool(self):
        for changed in ({'guid':{'value':'999'}},{'origin':{'value':'-'}}):
            old=self.prior();p=old['pools'][0]
            p['datasets']['tank/data/vm-101-disk-0']['origin']={'value':'tank/template@old'}
            current=copy.deepcopy(old)
            current['pools'][0]['datasets']['tank/data/vm-101-disk-0'].update(changed)
            self.choose(current,old)
            self.assertEqual(current['pools'][0]['replication_type'],'full')
            self.assertNotIn('replication_type',old['pools'][0])

    def test_selected_restore_excludes_retired_subtree_but_keeps_required_datasets(self):
        m=native_fixture();p=m['pools'][0];native=p['native_dataset']
        actual=[native,native+'/ROOT',native+'/ROOT/ubuntu',native+'/removed',native+'/removed/child']
        with patch.object(z,'run',return_value='\n'.join(actual)):
            sender=z.native_send(p,m['snapshot'])
        self.assertEqual(sender,['zfs','send','-R','-b','-X',native+'/removed',native+'@'+m['snapshot']])
        self.assertNotIn('-s',sender)

    def test_old_restore_excludes_new_dataset_even_if_it_has_the_old_snapshot(self):
        old=native_fixture();p=old['pools'][0];native=p['native_dataset']
        with patch.object(z,'run',return_value='\n'.join([native,native+'/ROOT',native+'/ROOT/ubuntu',native+'/new'])):
            sender=z.native_send(p,old['snapshot'])
        self.assertIn(native+'/new',sender)
        self.assertEqual(sender[sender.index(native+'/new')-1],'-X')

    def test_missing_expected_dataset_fails_instead_of_silently_skipping(self):
        p=native_fixture()['pools'][0]
        with patch.object(z,'run',return_value=p['native_dataset']):
            with self.assertRaisesRegex(z.Error,'Missing datasets required'):
                z.native_send(p,'selected')

    def test_remote_estimate_and_send_apply_same_exclusions_on_server(self):
        remote=z.RemoteRepository('server');remote.dataset='store'
        m=native_fixture();p=m['pools'][0];native=p['native_dataset'];p['encrypted']=True
        listing='\n'.join([native,native+'/ROOT',native+'/ROOT/ubuntu',native+'/retired'])
        def run(*args,**kwargs):
            return listing if args[:2]==('zfs','list') else 'size\t4096\n'
        with patch.object(remote,'run',side_effect=run) as remote_run,patch.object(z,'run') as local:
            self.assertEqual(z.estimate_native_send(p,m['snapshot'],remote),4096)
            sender=z.native_send(p,m['snapshot'],remote)
        self.assertEqual(z.shlex.split(sender[-1])[2:],
                         ['zfs','send','-R','-b','-w','-X',native+'/retired',native+'@'+m['snapshot']])
        estimate=next(c.args for c in remote_run.call_args_list if c.args[:2]==('zfs','send'))
        self.assertEqual(estimate[4:],tuple(z.shlex.split(sender[-1])[4:]))
        local.assert_not_called()

    def test_native_inventory_rejects_paths_outside_replica(self):
        p=native_fixture()['pools'][0]
        with patch.object(z,'run',return_value=p['native_dataset']+'\nother/data'):
            with self.assertRaisesRegex(z.Error,'Invalid native dataset inventory'):z.native_dataset_names(p)


class PersistentDeviceTests(unittest.TestCase):
    """Use real symlinks, but regular files stand in for block devices."""
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.ids=self.root/'by-id';self.ids.mkdir()
        self.raw=self.root/'sdb';self.raw.touch()
        self.other=self.root/'sdc';self.other.touch()
        self.part=self.root/'sdb2';self.part.touch()
        for name,target in [('wwn-0x123',self.raw),('ata-Model_SERIAL',self.raw),
                            ('wwn-0x123-part2',self.part),('ata-Model_SERIAL-part2',self.part),
                            ('wwn-0x456',self.other)]:
            (self.ids/name).symlink_to(target)
        for mock in (patch.object(z,'DISK_IDS',self.ids),
                     patch.object(Path,'is_block_device',autospec=True,side_effect=lambda p:p.is_file())):
            mock.start();self.addCleanup(mock.stop)
        self.disk=str(self.ids/'wwn-0x123')
        self.partition=str(self.ids/'wwn-0x123-part2')

    def test_kernel_input_binds_to_wwn_and_explicit_alias_is_retained(self):
        self.assertEqual(z.stable_device(self.raw),self.disk)
        ata=str(self.ids/'ata-Model_SERIAL')
        self.assertEqual(z.stable_device(ata),ata)
        self.assertEqual(z.stable_device(self.part),self.partition)

    def test_selected_id_survives_kernel_name_change_and_reuse(self):
        selected=z.stable_device(self.raw)
        renamed=self.root/'sdd'
        self.raw.rename(renamed)
        self.raw.write_text('different disk now using old kernel name')
        for name in ('wwn-0x123','ata-Model_SERIAL'):
            link=self.ids/name;link.unlink();link.symlink_to(renamed)
        self.assertEqual(z.stable_device(selected),selected)
        self.assertEqual(z.os.path.realpath(selected),str(renamed))
        with self.assertRaisesRegex(z.Error,'no persistent'):
            z.stable_device(self.raw)

    def test_missing_selected_id_does_not_fall_back_to_another_alias(self):
        Path(self.disk).unlink()
        with self.assertRaisesRegex(z.Error,'no persistent'):
            z.stable_device(self.disk)
        self.assertTrue((self.ids/'ata-Model_SERIAL').exists())

    def test_missing_broken_or_non_hardware_ids_are_rejected(self):
        unknown=self.root/'sde';unknown.touch()
        (self.ids/'lvm-pv-uuid-copyable').symlink_to(unknown)
        (self.ids/'wwn-broken').symlink_to(self.root/'absent')
        for path in (unknown,self.ids/'wwn-broken'):
            with self.subTest(path=path),self.assertRaisesRegex(z.Error,'no persistent'):
                z.stable_device(path)

    def test_inventory_displays_ids_and_retains_unidentified_disks(self):
        unknown=self.root/'sde';unknown.touch()
        inv={'blockdevices':[dict(path=str(self.raw),type='disk',children=[
            dict(path=str(self.part),type='part')]),dict(path=str(unknown),type='disk')]}
        with patch.object(z,'run',return_value=json.dumps(inv)):
            found=z.inventory()['blockdevices']
        self.assertEqual(found[0]['path'],self.disk)
        self.assertEqual(found[0]['children'][0]['path'],self.partition)
        self.assertIn('no persistent',found[1]['device_id_error'])

    def test_partition_uses_matching_disk_alias_and_checks_parent_and_number(self):
        for disk,expected in ((self.disk,self.partition),
                              (str(self.ids/'ata-Model_SERIAL'),str(self.ids/'ata-Model_SERIAL-part2'))):
            with self.subTest(disk=disk),patch.object(z,'node_for',side_effect=[{'type':'disk'},{'type':'part'}]), \
                 patch.object(z,'disk_for',return_value=self.disk),patch.object(Path,'read_text',return_value='2'):
                self.assertEqual(z.partition_device(disk,2),expected)
        with patch.object(z,'node_for',side_effect=[{'type':'disk'},{'type':'part'}]), \
             patch.object(z,'disk_for',return_value=str(self.ids/'wwn-0x456')):
            with self.assertRaisesRegex(z.Error,'does not belong'):
                z.partition_device(self.disk,2)
        with patch.object(z,'node_for',side_effect=[{'type':'disk'},{'type':'part'}]), \
             patch.object(z,'disk_for',return_value=self.disk),patch.object(Path,'read_text',return_value='3'):
            with self.assertRaisesRegex(z.Error,'wrong partition number'):
                z.partition_device(self.disk,2)

    def test_partition_does_not_fall_back_to_raw_name_or_other_alias(self):
        Path(self.partition).unlink()
        with patch.object(z,'node_for',return_value={'type':'disk'}),patch.object(z.time,'sleep') as sleep:
            with self.assertRaisesRegex(z.Error,'no persistent'):
                z.partition_device(self.disk,2)
        self.assertEqual(sleep.call_count,20)

    def test_partition_waits_for_selected_alias_then_checks_identity(self):
        Path(self.partition).unlink()
        def udev_creates_alias(delay):
            self.assertEqual(delay,0.1)
            Path(self.partition).symlink_to(self.part)
        with patch.object(z,'node_for',side_effect=[{'type':'disk'},{'type':'part'}]), \
             patch.object(z,'disk_for',return_value=self.disk), \
             patch.object(Path,'read_text',return_value='2'), \
             patch.object(z.time,'sleep',side_effect=udev_creates_alias) as sleep:
            self.assertEqual(z.partition_device(self.disk,2),self.partition)
        sleep.assert_called_once_with(0.1)

    def test_late_partition_alias_with_wrong_parent_is_rejected(self):
        Path(self.partition).unlink()
        with patch.object(z,'node_for',side_effect=[{'type':'disk'},{'type':'part'}]), \
             patch.object(z,'disk_for',return_value=str(self.ids/'wwn-0x456')), \
             patch.object(z.time,'sleep',side_effect=lambda delay:Path(self.partition).symlink_to(self.part)):
            with self.assertRaisesRegex(z.Error,'does not belong'):
                z.partition_device(self.disk,2)

    def test_import_search_links_to_persistent_partition_id(self):
        with z.pool_search_directory(self.part) as directory:
            link=Path(directory)/Path(self.partition).name
            self.assertEqual(str(link.readlink()),self.partition)

    def test_pool_leaves_accept_scoped_import_path_and_return_hardware_ids(self):
        with z.pool_search_directory(self.part) as directory:
            member=str(Path(directory)/Path(self.partition).name)
            status=f'config:\n NAME STATE READ WRITE CKSUM\n pool ONLINE 0 0 0\n {member} ONLINE 0 0 0\nerrors: No known data errors'
            with patch.object(z,'run',return_value=status),patch.object(z,'disk_for',return_value=self.disk):
                self.assertEqual(z.pool_leaves('pool'),[self.partition])

    def test_layout_commands_keep_selected_id(self):
        with patch.object(z,'node_for',return_value={'log-sec':512,'size':z.GIB}), \
             patch.object(z,'read_gpt',return_value={'entry_count':128,'partitions':[]}) as read, \
             patch.object(z,'run') as run:
            z.create_layout(str(self.ids/'ata-Model_SERIAL'),[],128)
        selected=str(self.ids/'ata-Model_SERIAL')
        disk_commands=[c.args for c in run.call_args_list if c.args[0] in ('sgdisk','partprobe')]
        self.assertTrue(disk_commands)
        self.assertTrue(all(command[-1]==selected for command in disk_commands))
        run.assert_any_call('partprobe',selected)
        self.assertEqual(run.call_args_list[-1].args,('udevadm','settle'))
        for call in read.call_args_list:
            self.assertEqual(call.args,(selected,512,z.GIB))

    def test_missing_id_stops_layout_before_any_command(self):
        Path(self.disk).unlink()
        with patch.object(z,'run') as run,patch.object(z,'read_gpt') as read:
            with self.assertRaises(z.Error):z.create_layout(self.disk,[],128)
            run.assert_not_called();read.assert_not_called()

    def test_unidentified_idle_disk_prevents_unattended_auto_selection(self):
        backup=dict(path=self.disk,type='disk',size=z.GIB,children=[
            dict(path=self.partition,type='part',fstype='zfs_member',label='linux_os_backup_test')])
        unknown=dict(path='/dev/unidentified',type='disk',size=z.GIB,device_id_error='no persistent ID')
        for disks in ([backup,unknown],[dict(backup,device_id_error='no persistent ID')]):
            with self.subTest(disks=disks),patch.object(z,'inventory',return_value={'blockdevices':disks}), \
                 patch.object(z,'target_idle'),patch('builtins.input',side_effect=AssertionError('prompted')), \
                 patch('sys.stdout',new_callable=io.StringIO),self.assertRaises(z.Error):
                z.choose_destination('Backup',allow_path=True,unattended=True)

    def test_protected_disk_alias_is_excluded_before_probing(self):
        protected=dict(path=self.disk,type='disk',size=z.GIB)
        with patch.object(z,'inventory',return_value={'blockdevices':[protected]}), \
             patch.object(z,'target_idle') as idle,patch('builtins.input',return_value='q'):
            with self.assertRaises(z.Cancelled):
                z.choose_destination('Restore',[str(self.ids/'ata-Model_SERIAL')])
            idle.assert_not_called()

    def test_backup_and_management_keep_id_when_opening_existing_store(self):
        node={'type':'disk','fstype':'zfs_member'}
        aliases=z.device_ids()
        # Only these boundaries stat the whole disk to reject directory inputs.
        with patch.object(z,'backup_writer_lock',return_value=z.contextlib.nullcontext()),patch.object(Path,'stat',return_value=z.os.stat_result((z.stat.S_IFBLK,)*10)), \
             patch.object(z,'device_ids',return_value=aliases), \
             patch.object(z,'node_for',return_value=node), \
             patch.object(z,'existing_store',return_value=z.contextlib.nullcontext('/repo')) as store:
            with z.backup_destination(self.raw,None,1,unattended=True) as repo:
                self.assertEqual(repo,'/repo')
            store.assert_called_once_with(self.disk,None)
            store.reset_mock()
            with z.management_storage(self.ids/'ata-Model_SERIAL') as repo:
                self.assertEqual(repo,'/repo')
            store.assert_called_once_with(str(self.ids/'ata-Model_SERIAL'),None)

    def test_nvme_namespace_id_is_preferred_to_obsolete_serial_alias(self):
        for name in ('nvme-Model_SERIAL','nvme-Model_SERIAL_1'):
            (self.ids/name).symlink_to(self.other)
        (self.ids/'wwn-0x456').unlink()
        self.assertEqual(z.stable_device(self.other),str(self.ids/'nvme-Model_SERIAL_1'))
        (self.ids/'nvme-eui.12345678').symlink_to(self.other)
        self.assertEqual(z.stable_device(self.other),str(self.ids/'nvme-eui.12345678'))

    def test_usb_serial_id_is_usable_without_wwn(self):
        (self.ids/'wwn-0x456').unlink()
        (self.ids/'usb-Model_SERIAL-0:0').symlink_to(self.other)
        self.assertEqual(z.stable_device(self.other),str(self.ids/'usb-Model_SERIAL-0:0'))

    def test_target_safety_opens_id_and_uses_kernel_name_only_for_sysfs(self):
        node=dict(path=str(self.raw),type='disk',children=[dict(path=str(self.part),type='part')])
        aliases=z.device_ids()
        with patch.object(z,'device_ids',return_value=aliases),patch.object(z,'node_for',return_value=node), \
             patch.object(z.os,'stat',return_value=z.os.stat_result((z.stat.S_IFBLK,)*10)), \
             patch.object(Path,'read_text',return_value='Filename Type Size Used Priority\n'), \
             patch.object(Path,'is_dir',autospec=True,return_value=True) as directory, \
             patch.object(Path,'iterdir',side_effect=lambda:iter([])), \
             patch.object(z,'run',return_value=''),patch.object(z.os,'open',return_value=7) as opened, \
             patch.object(z.os,'close') as close:
            result=z.target_idle(self.disk)
        opened.assert_called_once_with(self.disk,z.os.O_RDONLY|z.os.O_EXCL)
        close.assert_called_once_with(7)
        self.assertEqual(result['path'],self.disk)
        self.assertEqual(result['children'][0]['path'],self.partition)
        self.assertEqual([c.args[0] for c in directory.call_args_list],
                         [Path('/sys/class/block/sdb/holders'),Path('/sys/class/block/sdb2/holders')])

    def test_existing_store_probes_by_id_and_accepts_other_alias_of_same_member(self):
        name='linux_os_backup_test'
        node=dict(path=str(self.raw),type='disk',children=[
            dict(path=str(self.part),type='part',fstype='zfs_member')])
        def read(*args,**kwargs):
            if args[0]=='blkid':
                self.assertEqual(args[-1],self.partition)
                return f'LABEL={name}\nUUID=123\n'
            if args[:2]==('zpool','list'):return name+'\n'
            if 'mountpoint' in args:return str(self.root)+'\n'
            if args[0] in ('mount','umount'):return ''
            self.fail(f'Unexpected command {args}')
        with patch.object(z,'private_storage_namespace'),patch.object(z,'node_for',return_value=node),patch.object(z,'run',side_effect=read), \
             patch.object(z,'props',return_value={name:{'guid':{'value':'123'}}}), \
             patch.object(z,'pool_leaves',return_value=[str(self.ids/'ata-Model_SERIAL-part2')]):
            with z.existing_store(self.raw,None) as repository:
                self.assertNotEqual(repository,self.root)
                self.assertTrue(repository.is_dir())


def incremental_restore_fixture():
    m=native_fixture();pool=m['pools'][0]
    m['snapshot']='system-backup-20260916-120000'
    base='system-backup-20260915-120000'
    def entry(kind,guid,txg):
        return dict(type=kind,guid=str(guid),txg=txg,origin='-',resume='-',mounted='no',holds='0',clones='-')
    source={};target={};pool['datasets']={}
    for i,relative in enumerate(('', '/ROOT', '/ROOT/ubuntu')):
        source[relative]=entry('filesystem',1000+i,1)
        target[relative]=entry('filesystem',2000+i,1)
        source[relative+'@'+base]=entry('snapshot',100+i,10)
        target[relative+'@'+base]=entry('snapshot',100+i,10)
        source[relative+'@'+m['snapshot']]=entry('snapshot',200+i,30)
        target[relative+'@target-only']=entry('snapshot',300+i,20)
        pool['datasets'][pool['name']+relative]={'encryption':{'value':'off'}}
        pool['datasets'][pool['name']+relative+'@'+m['snapshot']]={'guid':{'value':str(200+i)}}
    return m,source,target,base,entry


class IncrementalRestorePlanTests(unittest.TestCase):
    def setUp(self):
        self.m,self.source,self.target,self.base,self.entry=incremental_restore_fixture()
        self.pool=self.m['pools'][0]

    def plan(self):
        return z.incremental_pool_plan(self.pool,self.m['snapshot'],self.source,self.target)

    def test_shared_guids_select_base_and_list_exact_target_snapshot_removals(self):
        plan=self.plan()
        self.assertEqual(plan['base'],self.base)
        self.assertEqual(plan['remove_snapshots'],['/ROOT/ubuntu@target-only','/ROOT@target-only','@target-only'])
        self.assertEqual(set(plan['rollbacks']),{'','/ROOT','/ROOT/ubuntu'})
        self.assertEqual(plan['remove_datasets'],[])

    def test_matching_names_with_different_guids_are_not_common_snapshots(self):
        self.target['@'+self.base]['guid']='999'
        with self.assertRaisesRegex(z.Error,'no usable common snapshot'):self.plan()

    def test_shared_root_without_child_base_aborts(self):
        del self.target['/ROOT/ubuntu@'+self.base]
        with self.assertRaisesRegex(z.Error,'no usable common snapshot'):self.plan()

    def test_newest_usable_common_snapshot_wins(self):
        for relative in ('','/ROOT','/ROOT/ubuntu'):
            name=relative+'@newer-base'
            self.source[name]=self.entry('snapshot',500+len(relative),25)
            self.target[name]=copy.deepcopy(self.source[name])
        self.assertEqual(self.plan()['base'],'newer-base')

    def test_source_snapshots_after_selected_point_cannot_be_bases(self):
        for relative in ('','/ROOT','/ROOT/ubuntu'):
            name=relative+'@future'
            self.source[name]=self.entry('snapshot',700+len(relative),40)
            self.target[name]=copy.deepcopy(self.source[name])
        self.assertEqual(self.plan()['base'],self.base)
        self.assertIn('@future',self.plan()['remove_snapshots'])

    def test_renamed_target_snapshot_matches_by_guid(self):
        for relative in ('','/ROOT','/ROOT/ubuntu'):
            self.target[relative+'@renamed']=self.target.pop(relative+'@'+self.base)
        plan=self.plan()
        self.assertTrue(all(n.endswith('@renamed') for n in plan['rollbacks'].values()))

    def test_selected_snapshot_already_present_plans_rollback_without_transfer(self):
        self.target=copy.deepcopy(self.source)
        self.target['@local-later']=self.entry('snapshot',901,40)
        plan=self.plan()
        self.assertEqual(plan['base'],self.m['snapshot'])
        self.assertEqual(set(plan['remove_snapshots']),{'@local-later',*{r+'@'+self.base for r in ('','/ROOT','/ROOT/ubuntu')}})

    def test_no_common_snapshot_aborts_without_running_commands(self):
        self.target={n:p for n,p in self.target.items() if p['type']!='snapshot'}
        with patch.object(z,'run') as run:
            with self.assertRaisesRegex(z.Error,'no usable common'):self.plan()
            run.assert_not_called()

    def test_missing_target_dataset_with_existing_backup_base_cannot_be_full_replaced(self):
        self.target={n:p for n,p in self.target.items() if not n.startswith('/ROOT/ubuntu')}
        with self.assertRaisesRegex(z.Error,'no usable common'):self.plan()

    def test_new_dataset_can_be_seeded_as_part_of_incremental_replication(self):
        self.source['/new']=self.entry('filesystem',980,15)
        self.source['/new@'+self.m['snapshot']]=self.entry('snapshot',981,30)
        self.pool['datasets']['tank/new']={'encryption':{'value':'off'}}
        self.pool['datasets']['tank/new@'+self.m['snapshot']]={'guid':{'value':'981'}}
        self.assertNotIn('/new',self.plan()['rollbacks'])
        self.target['/new']=self.entry('filesystem',982,15)
        with self.assertRaisesRegex(z.Error,'no usable common'):self.plan()

    def test_retired_target_tree_is_listed_once(self):
        for suffix in ('/retired','/retired/child'):
            self.target[suffix]=self.entry('filesystem',400,1)
            self.target[suffix+'@old']=self.entry('snapshot',401,10)
        plan=self.plan()
        self.assertEqual(plan['remove_datasets'],['/retired'])
        self.assertIn('/retired/child@old',plan['remove_snapshots'])

    def test_holds_and_clone_dependencies_block_required_removals(self):
        for field,value in [('holds','1'),('clones','/dependent')]:
            with self.subTest(field=field):
                self.target['@target-only'][field]=value
                with self.assertRaisesRegex(z.Error,'holds or dependent clones'):self.plan()
                self.target['@target-only'][field]='0' if field=='holds' else '-'

    def test_held_common_base_can_be_preserved(self):
        self.target['@'+self.base]['holds']='1'
        self.assertEqual(self.plan()['base'],self.base)

    def test_incomplete_receive_or_mounted_dataset_aborts(self):
        for field,value in [('resume','token'),('mounted','yes')]:
            old=self.target[''][field];self.target[''][field]=value
            with self.subTest(field=field),self.assertRaisesRegex(z.Error,'mounted datasets or an incomplete receive'):self.plan()
            self.target[''][field]=old

    def test_selected_snapshot_guid_is_revalidated(self):
        self.source['/ROOT@'+self.m['snapshot']]['guid']='991'
        with self.assertRaisesRegex(z.Error,'Selected backup snapshot identity changed'):self.plan()

    def test_dataset_type_mismatch_aborts(self):
        self.target['/ROOT/ubuntu']['type']='volume'
        with self.assertRaisesRegex(z.Error,'no usable common'):self.plan()


class IncrementalRestoreLayoutTests(unittest.TestCase):
    def setUp(self):
        self.m=ubuntu_fixture();self.geometry=copy.deepcopy(self.m['disk'])
        self.node={'log-sec':512,'size':self.geometry['size_bytes']}
        self.device='/dev/disk/by-id/wwn-target'

    def inspect(self,guids=None):
        def label(*args,**kwargs):
            number=int(args[-1].rsplit('-part',1)[1])
            p=next(p for p in self.m['disk']['partitions'] if p['number']==number)
            if p['kind']=='zfs':
                pool=next(pool for pool in self.m['pools'] if pool['name']==p['pool'])
                guid=(guids or {}).get(pool['name'],pool['guid'])
                return f'TYPE=zfs_member\nLABEL={pool["name"]}\nUUID={guid}\n'
            prefix='fat' if p['kind']=='esp' else 'swap'
            return f'TYPE={"vfat" if prefix=="fat" else "swap"}\nUUID={p[prefix+"_uuid"]}\nLABEL={p[prefix+"_label"]}\n'
        with patch.object(z,'read_gpt',return_value=self.geometry),patch.object(z,'run',side_effect=label), \
             patch.object(z,'partition_device',side_effect=lambda d,n:d+'-part'+str(n)):
            return z.incremental_layout(self.m,self.device,self.node)

    def test_partition_size_and_resulting_offset_changes_are_preserved(self):
        self.geometry['partitions'][2]['end_lba']+=2048
        self.geometry['partitions'][2]['size_bytes']+=z.MIB
        self.geometry['partitions'][3]['start_lba']+=2048
        self.geometry['partitions'][3]['size_bytes']-=z.MIB
        actual=self.inspect()
        self.assertEqual(actual,self.geometry['partitions'])
        self.assertNotEqual(actual,self.m['disk']['partitions'])

    def test_partition_order_number_type_uuid_and_attributes_must_match(self):
        for key,value in [('number',99),('type_guid',z.ZFS),('partuuid',str(uuid.uuid4())),('name','wrong'),('attributes',1)]:
            old=self.geometry['partitions'][0][key]
            self.geometry['partitions'][0][key]=value
            with self.subTest(key=key),self.assertRaisesRegex(z.Error,'Incremental restore: partition'):self.inspect()
            self.geometry['partitions'][0][key]=old
        self.geometry['partitions'].reverse()
        with self.assertRaisesRegex(z.Error,'numbers/order'):self.inspect()

    def test_missing_partition_aborts(self):
        self.geometry['partitions'].pop()
        with self.assertRaisesRegex(z.Error,'numbers/order'):self.inspect()

    def test_sector_size_mismatch_aborts(self):
        self.node['log-sec']=4096
        with self.assertRaisesRegex(z.Error,'sector sizes'):self.inspect()

    def test_wrong_esp_identity_aborts(self):
        with patch.object(z,'read_gpt',return_value=self.geometry), \
             patch.object(z,'partition_device',return_value='part'),patch.object(z,'run',return_value='TYPE=vfat\nUUID=BAD0-0000'):
            with self.assertRaisesRegex(z.Error,'filesystem identity'):z.incremental_layout(self.m,self.device,self.node)

    def test_new_pool_identities_are_discovered_and_must_remain_stable(self):
        guids={'bpool':'888','rpool':'999'}
        self.assertEqual(self.inspect(guids),self.geometry['partitions'])
        self.assertEqual({p['name']:p['_restore_guid'] for p in self.m['pools']},guids)
        self.inspect(guids)
        with self.assertRaisesRegex(z.Error,'identity changed during confirmation'):
            self.inspect(dict(guids,rpool='1000'))

    def test_invalid_pool_identity_aborts(self):
        for guid in ('','abc','0',str(2**64)):
            with self.subTest(guid=guid),self.assertRaisesRegex(z.Error,'pool identity differs'):
                self.inspect({'bpool':guid})


class IncrementalRestoreWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.m,self.source,self.target,self.base,self.entry=incremental_restore_fixture()
        self.pool=self.m['pools'][0]
        self.device='/dev/disk/by-id/wwn-target'
        self.plan=z.incremental_pool_plan(self.pool,self.m['snapshot'],self.source,self.target)
        self.plan.update(source=self.source,bytes=4096)
        self.plans={self.pool['name']:self.plan}
        self.node=dict(size=z.GIB,**{'log-sec':512,'phy-sec':512,'serial':'test','wwn':'test','maj:min':'8:16'})

    def exercise(self,dry=False,answer=None,plans=None,layout_error=False):
        modes=[]
        @z.contextlib.contextmanager
        def pools(m,device,readonly=True):
            modes.append(readonly);yield [(self.pool,'temporary')]
        args=z.argparse.Namespace(target=self.device,dry_run=dry)
        with patch.object(z,'guid_conflicts',return_value=[]),patch.object(z,'protected_path',return_value=set()), \
             patch.object(z,'stable_device',side_effect=str),patch.object(z,'target_idle',return_value=self.node), \
             patch.object(z,'incremental_layout',side_effect=z.Error('layout mismatch') if layout_error else None,return_value=[]) as layout, \
             patch.object(z,'incremental_pools',side_effect=pools), \
             patch.object(z,'incremental_plans',side_effect=plans,return_value=self.plans), \
             patch.object(z,'incremental_boot_files',return_value=z.contextlib.nullcontext({})), \
             patch.object(z,'print_existing_layout'),patch.object(z,'apply_incremental_restore') as apply, \
             patch.object(z,'create_layout') as erase,patch.object(z,'clone_restore') as full, \
             patch('builtins.input',return_value=answer or 'INCREMENTAL RESTORE '+self.device) as prompt, \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            try:z.incremental_restore_from_storage(args,Path('/repo'),self.m)
            finally:
                erase.assert_not_called();full.assert_not_called()
                if dry or layout_error or plans or answer=='decline':apply.assert_not_called()
        return modes,prompt,apply,output.getvalue()

    def test_dry_run_uses_readonly_import_and_no_confirmation(self):
        modes,prompt,apply,output=self.exercise(dry=True)
        self.assertEqual(modes,[True]);prompt.assert_not_called();apply.assert_not_called()
        self.assertIn('tank/ROOT/ubuntu@target-only',output)
        self.assertIn('INCREMENTAL RESTORE '+self.device,output)

    def test_real_restore_requires_confirmation_and_readonly_recheck_before_writes(self):
        modes,prompt,apply,output=self.exercise()
        self.assertEqual(modes,[True,True,False]);prompt.assert_called_once();apply.assert_called_once()
        self.assertIn('Remove target snapshot: tank@target-only',output)

    def test_declining_confirmation_aborts_without_writable_import(self):
        with self.assertRaisesRegex(z.Error,'Confirmation did not match'):self.exercise(answer='decline')

    def test_no_common_snapshot_never_falls_back_to_full_restore(self):
        with self.assertRaisesRegex(z.Error,'no common snapshot'):
            self.exercise(plans=z.Error('no common snapshot'))

    def test_layout_mismatch_never_reaches_receive(self):
        with self.assertRaisesRegex(z.Error,'layout mismatch'):self.exercise(layout_error=True)

    def test_snapshot_change_during_confirmation_aborts(self):
        changed=copy.deepcopy(self.plans);changed['tank']['target']['']['guid']='999'
        with self.assertRaisesRegex(z.Error,'changed during confirmation'):
            self.exercise(plans=[self.plans,changed])

    def test_running_target_identity_blocks_import_without_exporting_live_pool(self):
        with patch.object(z,'run',return_value='tank\t123') as run:
            with self.assertRaisesRegex(z.Error,'target pool identity is already imported'):
                with z.incremental_pools(self.m,self.device):self.fail('Imported live pool')
            self.assertEqual(run.call_args_list,[unittest.mock.call('zpool','list','-H','-o','name,guid')])

    def test_incremental_entry_bypasses_full_estimates_layout_solver_and_swap_prompt(self):
        args=z.argparse.Namespace(target=self.device,incremental=True,snapshot=None,dry_run=True)
        with patch.object(z,'select_backup',return_value=Path('/repo')), \
             patch.object(z,'verify_chain',return_value=[(Path('/repo'),self.m)]), \
             patch.object(z,'commands'),patch.object(z,'run'),patch.object(z,'incremental_restore_from_storage') as restore, \
             patch.object(z,'estimate_native_send') as estimate,patch.object(z,'solve') as solve, \
             patch.object(z,'choose_restore_swap_sizes') as swap:
            z.restore_from_storage(args,Path('/repo'))
            restore.assert_called_once_with(args,Path('/repo'),self.m,None)
            estimate.assert_not_called();solve.assert_not_called();swap.assert_not_called()

    def test_restore_flag_parser_and_other_commands(self):
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'restore') as restore:
            self.assertEqual(z.main(['restore','--incremental','--dry-run'],restore_isolated=True),0)
            self.assertTrue(restore.call_args.args[0].incremental)
        for command in ('verify','snapshots','clone-backup'):
            with self.subTest(command=command),patch('sys.stderr',new_callable=io.StringIO),self.assertRaises(SystemExit) as error:
                z.main([command,'--incremental'])
            self.assertEqual(error.exception.code,2)

    def test_incremental_send_includes_base_and_preserves_encryption_and_exclusions(self):
        pool=copy.deepcopy(self.pool);pool['encrypted']=True
        with patch.object(z,'native_dataset_names',return_value=z.native_expected_names(pool)|{pool['native_dataset']+'/retired'}):
            argv=z.incremental_send_argv(pool,self.m['snapshot'],self.base)
        self.assertIn('-R',argv);self.assertIn('-w',argv)
        self.assertEqual(argv[-3:],['-i',pool['native_dataset']+'@'+self.base,pool['native_dataset']+'@'+self.m['snapshot']])
        self.assertIn('-X',argv)

    def test_apply_uses_incremental_stream_and_no_partition_or_pool_creation(self):
        calls=[]
        with patch.object(z,'run',side_effect=lambda *a,**kw:calls.append(a) or ('ONLINE' if 'health' in a else '')), \
             patch.object(z,'native_dataset_names',return_value=z.native_expected_names(self.pool)), \
             patch.object(z,'pipe_transfer',side_effect=lambda *a:calls.append(('receive',)) or 4096) as pipe, \
             patch.object(z,'incremental_inventory',return_value=self.source), \
             patch.object(z,'restore_mount_properties'), \
             patch.object(z,'update_incremental_boot',side_effect=lambda *a:calls.append(('boot-files',))) as boot, \
             patch.object(z,'refresh_restored_dracut',side_effect=lambda *a:calls.append(('initramfs',))) as dracut:
            z.apply_incremental_restore(Path('/repo'),self.m,self.device,[],[(self.pool,'temp')],self.plans,{})
        self.assertEqual(sum(c[:2]==('zfs','rollback') for c in calls),3)
        sender,receiver=pipe.call_args.args[:2]
        self.assertIn('-i',sender);self.assertEqual(sender[-2],self.pool['native_dataset']+'@'+self.base)
        self.assertEqual(receiver[:4],['zfs','receive','-u','-F']);self.assertEqual(receiver[-1],'temp')
        self.assertNotIn('compression',receiver)
        self.assertIn(('zfs','set','compression=lz4','temp'),calls)
        self.assertFalse(any('compression=off' in c for c in calls))
        self.assertFalse(any(c[0] in ('sgdisk','mkfs.fat','mkswap') or c[:2]==('zpool','create') for c in calls))
        boot.assert_called_once()
        dracut.assert_called_once_with(self.m,[(self.pool,'temp')],self.device,[])
        self.assertLess(calls.index(('receive',)),calls.index(('boot-files',)))
        self.assertLess(calls.index(('boot-files',)),calls.index(('initramfs',)))
        self.assertLess(calls.index(('zfs','set','sync=disabled','temp')),calls.index(('receive',)))
        self.assertLess(calls.index(('initramfs',)),calls.index(('zfs','set','sync=standard','temp')))
        self.assertLess(calls.index(('zfs','set','sync=standard','temp')),len(calls)-1)

    def test_already_received_point_rolls_back_and_renames_without_sending(self):
        target=copy.deepcopy(self.source)
        for relative in ('','/ROOT','/ROOT/ubuntu'):
            target[relative+'@renamed']=target.pop(relative+'@'+self.m['snapshot'])
        plan=z.incremental_pool_plan(self.pool,self.m['snapshot'],self.source,target)
        plan.update(source=self.source,bytes=0)
        with patch.object(z,'run',return_value='ONLINE') as run,patch.object(z,'pipe_transfer') as pipe, \
             patch.object(z,'incremental_inventory',return_value=self.source),patch.object(z,'restore_mount_properties'), \
             patch.object(z,'update_incremental_boot'),patch.object(z,'refresh_restored_dracut') as dracut:
            transferred=z.apply_incremental_restore(Path('/repo'),self.m,self.device,[],[(self.pool,'temp')],{'tank':plan},{})
        self.assertEqual((transferred.stream,transferred.compressed,transferred.uncompressed),(0,0,0))
        pipe.assert_not_called()
        dracut.assert_called_once_with(self.m,[(self.pool,'temp')],self.device,[])
        self.assertEqual(sum(c.args[:2]==('zfs','rename') for c in run.call_args_list),3)

    def test_incremental_compression_changes_existing_datasets_and_new_receive(self):
        z.restore_compression(self.m,'gzip-6')
        reads=[]
        def response(*args,**kwargs):
            if args[:2]==('zfs','list') and args[-2].endswith(',compressratio'):
                return f'temp\t{z.GIB}\t0\t{3*z.GIB}\t3.21\n'
            if args[:2]==('zfs','list') and args[-2]=='name,type,guid,createtxg,referenced,origin':
                reads.append(args)
                rows=f'temp\tfilesystem\t1\t1\t0\t-\ntemp@base\tsnapshot\t10\t2\t{z.GIB}\t-\n'
                if len(reads)>1:rows+=f'temp@tip\tsnapshot\t11\t3\t{z.GIB-100}\t-\n'
                return rows
            if args[:2]==('zfs','get') and args[-2]=='written@temp@base':return '1500000'
            return 'ONLINE'
        with patch.object(z,'run',side_effect=response) as run,patch.object(z,'pipe_transfer',return_value=4000000) as pipe, \
             patch.object(z,'native_dataset_names',return_value=z.native_expected_names(self.pool)), \
             patch.object(z,'incremental_inventory',return_value=self.source),patch.object(z,'restore_mount_properties'), \
             patch.object(z,'update_incremental_boot'),patch.object(z,'refresh_restored_dracut'), \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            transferred=z.apply_incremental_restore(Path('/repo'),self.m,self.device,[],[(self.pool,'temp')],self.plans,{})
        self.assertEqual(transferred.stream,4000000)
        self.assertEqual(transferred.uncompressed,4000000)
        self.assertEqual(transferred.compressed,1500000)
        self.assertIn('compression=gzip-6',pipe.call_args.args[1])
        run.assert_any_call('zfs','set','compression=gzip-6','temp','temp/ROOT','temp/ROOT/ubuntu')
        calls=[c.args for c in run.call_args_list]
        query=('zfs','list','-H','-p','-r','-t','filesystem,volume','-o',
               'name,usedbydataset,usedbysnapshots,logicalused,compressratio','temp')
        self.assertEqual(calls[-1],query)
        self.assertEqual(calls[-2],('zpool','sync','temp'))
        self.assertIn('tank: compressed 1.000 GiB, uncompressed 3.000 GiB | ratio 3.21x',output.getvalue())

    def test_incremental_compression_applies_even_without_a_transfer(self):
        z.restore_compression(self.m,'gzip-6')
        plan=z.incremental_pool_plan(self.pool,self.m['snapshot'],self.source,self.source)
        plan.update(source=self.source,bytes=0)
        with patch.object(z,'run',return_value='ONLINE') as run,patch.object(z,'pipe_transfer') as pipe, \
             patch.object(z,'incremental_inventory',return_value=self.source),patch.object(z,'restore_mount_properties'), \
             patch.object(z,'update_incremental_boot'),patch.object(z,'refresh_restored_dracut'):
            z.apply_incremental_restore(Path('/repo'),self.m,self.device,[],[(self.pool,'temp')],{'tank':plan},{})
        pipe.assert_not_called()
        run.assert_any_call('zfs','set','compression=gzip-6','temp','temp/ROOT','temp/ROOT/ubuntu')

    def test_incremental_boot_pool_never_gets_a_compression_override(self):
        self.m['root_dataset']='other/ROOT/os'
        self.m['mounts']=[dict(source='tank/ROOT/ubuntu',target='/boot',fstype='zfs')]
        self.pool['_restore_compression']='gzip-6'
        with patch.object(z,'run',return_value='ONLINE') as run,patch.object(z,'pipe_transfer') as pipe, \
             patch.object(z,'native_dataset_names',return_value=z.native_expected_names(self.pool)), \
             patch.object(z,'incremental_inventory',return_value=self.source),patch.object(z,'restore_mount_properties'), \
             patch.object(z,'update_incremental_boot'),patch.object(z,'refresh_restored_dracut'):
            z.apply_incremental_restore(Path('/repo'),self.m,self.device,[],[(self.pool,'temp')],self.plans,{})
        self.assertFalse(any(a.startswith('compression=') for c in run.call_args_list for a in c.args))
        self.assertFalse(any(a.startswith('compression=') for a in pipe.call_args.args[1]))

    def test_imports_are_scoped_readonly_temporary_and_exported_on_failure(self):
        member=self.device+'-part2'
        with patch.object(z,'partition_device',return_value=member), \
             patch.object(z,'pool_search_directory',return_value=z.contextlib.nullcontext('/scoped')), \
             patch.object(z,'pool_guid',return_value=self.pool['guid']), \
             patch.object(z,'pool_leaves',return_value=[member]),patch.object(z,'run') as run:
            with self.assertRaisesRegex(z.Error,'planned failure'):
                with z.incremental_pools(self.m,self.device):raise z.Error('planned failure')
        imported=next(c.args for c in run.call_args_list if c.args[:2]==('zpool','import'))
        self.assertEqual(imported[:5],('zpool','import','-N','-t','-f'))
        self.assertIn('readonly=on',imported);self.assertIn('cachefile=none',imported)
        altroot=Path(imported[imported.index('-R')+1])
        self.assertTrue(altroot.is_absolute())
        self.assertTrue(altroot.name.startswith('lllzorb-target-'))
        self.assertFalse(altroot.exists())
        self.assertEqual(imported[-4:-1],('-d','/scoped',self.pool['guid']))
        self.assertEqual(run.call_args_list[-1].args,('zpool','export',imported[-1]))

    def test_new_target_identity_imports_while_original_is_running(self):
        self.pool['_restore_guid']='456'
        member=self.device+'-part2'
        def execute(*args):
            if args[:2]==('zpool','list'):return 'tank\t123'
            return ''
        with patch.object(z,'partition_device',return_value=member), \
             patch.object(z,'pool_search_directory',return_value=z.contextlib.nullcontext('/scoped')), \
             patch.object(z,'pool_guid',return_value='456'),patch.object(z,'pool_leaves',return_value=[member]), \
             patch.object(z,'run',side_effect=execute) as run:
            with z.incremental_pools(self.m,self.device) as restored:
                self.assertEqual(restored[0][0]['guid'],'123')
        imported=next(c.args for c in run.call_args_list if c.args[:2]==('zpool','import'))
        self.assertEqual(imported[-2],'456')
        self.assertEqual([c.args for c in run.call_args_list if c.args[:2]==('zpool','export')],
                         [('zpool','export',imported[-1])])

    def test_distinct_targets_of_same_source_can_be_imported_concurrently(self):
        other=copy.deepcopy(self.m)
        self.pool['_restore_guid']='456';other['pools'][0]['_restore_guid']='789'
        imported={'tank':'123'};members={}
        def execute(*args):
            if args[:2]==('zpool','list'):return '\n'.join(n+'\t'+g for n,g in imported.items())
            if args[:2]==('zpool','import'):
                imported[args[-1]]=args[-2];members[args[-1]]=args[args.index('-d')+1]
            if args[:2]==('zpool','export'):del imported[args[-1]]
            return ''
        with patch.object(z,'partition_device',side_effect=lambda d,n:d+'-part'+str(n)), \
             patch.object(z,'pool_search_directory',side_effect=z.contextlib.nullcontext), \
             patch.object(z,'pool_guid',side_effect=lambda alias:imported[alias]), \
             patch.object(z,'pool_leaves',side_effect=lambda alias:[members[alias]]), \
             patch.object(z,'run',side_effect=execute):
            with z.incremental_pools(self.m,self.device):
                with z.incremental_pools(other,self.device+'-other'):
                    self.assertEqual(set(imported.values()),{'123','456','789'})
        self.assertEqual(imported,{'tank':'123'})

    def test_local_and_remote_inventory_normalize_temporary_aliases(self):
        def listing(root):
            return '\n'.join(['\t'.join([root,'filesystem','100','1','-','-','no','-','-']),
                '\t'.join([root+'@base','snapshot','101','2','-','-','-','0',root+'/clone']),
                '\t'.join([root+'/clone','filesystem','102','3',root+'@base','-','no','-','-'])])
        remote=z.RemoteRepository('server','tank')
        with patch.object(z,'run',return_value=listing('one')):
            local=z.incremental_inventory('one')
        with patch.object(remote,'run',return_value=listing('two')):
            other=z.incremental_inventory('two',remote)
        self.assertEqual(local,other)
        self.assertEqual(local['@base']['clones'],'/clone')
        self.assertEqual(local['/clone']['origin'],'@base')


class IncrementalBootFileTests(unittest.TestCase):
    def test_esp_sync_updates_changed_files_removes_obsolete_and_leaves_identical_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source';source.mkdir()
            target=Path(tmp)/'target';target.mkdir()
            for directory in (source,target):
                (directory/'same').write_bytes(b'same contents')
            (target/'same').touch();before=(target/'same').stat().st_mtime_ns
            (source/'changed').write_bytes(b'new');(target/'changed').write_bytes(b'old')
            (target/'obsolete').write_bytes(b'old kernel')
            (target/'file-to-directory').write_bytes(b'old')
            (source/'file-to-directory').mkdir();(source/'file-to-directory'/'kernel').write_bytes(b'kernel')
            (target/'directory-to-file').mkdir();(target/'directory-to-file'/'child').write_bytes(b'old')
            (source/'directory-to-file').write_bytes(b'new file')
            z.sync_boot_tree(source,target)
            self.assertEqual((target/'same').stat().st_mtime_ns,before)
            self.assertFalse((target/'obsolete').exists())
            self.assertEqual({str(p.relative_to(source)):p.read_bytes() for p in source.rglob('*') if p.is_file()},
                             {str(p.relative_to(target)):p.read_bytes() for p in target.rglob('*') if p.is_file()})

    def test_boot_update_preserves_gpt_and_swap_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'disk').mkdir()
            (root/'disk'/'first-megabyte.bin').write_bytes(b'A'*z.MIB)
            disk=root/'device';disk.write_bytes(b'B'*z.MIB)
            swap=root/'swap';swap.write_bytes(b'swap untouched')
            with patch.object(z,'partition_device',return_value=str(swap)),patch.object(z,'run') as run:
                z.update_incremental_boot(root,str(disk),[{'kind':'swap','number':1}],{})
            self.assertEqual(disk.read_bytes(),b'A'*440+b'B'*(z.MIB-440))
            self.assertEqual(swap.read_bytes(),b'swap untouched');run.assert_not_called()


class IncrementalRestorePreflightTests(unittest.TestCase):
    def setUp(self):
        self.m,self.source,self.target,self.base,self.entry=incremental_restore_fixture()
        self.pool=self.m['pools'][0]

    def plans(self,available=2*z.GIB,remote=None,features=None):
        def execute(*args,**kwargs):
            if args[:2]==('zfs','send'):return 'size\t4096\n'
            if 'available' in args:return str(available)
            self.fail('Unexpected command: '+repr(args))
        with patch.object(z,'incremental_inventory',side_effect=[self.source,self.target]), \
             patch.object(z,'native_dataset_names',return_value=z.native_expected_names(self.pool)), \
             patch.object(z,'props',return_value={'alias':features or {}}),patch.object(z,'run',side_effect=execute) as local:
            if remote:
                with patch.object(remote,'run',side_effect=execute) as server:
                    result=z.incremental_plans(self.m,[(self.pool,'alias')],remote)
                    self.assertIn('-i',server.call_args.args)
            else:result=z.incremental_plans(self.m,[(self.pool,'alias')])
        return result,local

    def test_estimate_is_incremental_and_target_capacity_is_checked_locally(self):
        plans,execute=self.plans()
        self.assertEqual(plans['tank']['bytes'],4096)
        send=next(c.args for c in execute.call_args_list if c.args[:2]==('zfs','send'))
        self.assertEqual(send[:4],('zfs','send','-n','-P'))
        self.assertEqual(send[-3:],('-i',self.pool['native_dataset']+'@'+self.base,self.pool['native_dataset']+'@'+self.m['snapshot']))

    def test_remote_estimate_uses_remote_sender(self):
        remote=z.RemoteRepository('server','store');remote.dataset='store'
        plans,local=self.plans(remote=remote)
        self.assertEqual(plans['tank']['bytes'],4096)
        self.assertFalse(any(c.args[:2]==('zfs','send') for c in local.call_args_list))

    def test_insufficient_free_space_aborts(self):
        with self.assertRaisesRegex(z.Error,'insufficient free space'):self.plans(available=64*z.MIB)

    def test_missing_pool_feature_aborts(self):
        self.pool['properties']['feature@large_blocks']={'value':'active'}
        with self.assertRaisesRegex(z.Error,'lacks required features'):self.plans()
        result,_=self.plans(features={'feature@large_blocks':{'value':'enabled'}})
        self.assertEqual(result['tank']['bytes'],4096)

    def test_origin_change_aborts_before_receive(self):
        self.target['/ROOT/ubuntu']['origin']='@'+self.base
        with self.assertRaisesRegex(z.Error,'clone origins differ'):
            z.incremental_pool_plan(self.pool,self.m['snapshot'],self.source,self.target)

    def test_second_pool_import_failure_exports_first_pool(self):
        m=ubuntu_fixture();imports=[];exports=[]
        def execute(*args,**kwargs):
            if args[:2]==('zpool','import'):
                if imports:raise z.Error('second pool failed')
                imports.append(args[-1])
            if args[:2]==('zpool','export'):exports.append(args[-1])
            return ''
        with patch.object(z,'partition_device',return_value='/dev/disk/by-id/wwn-test-part1'), \
             patch.object(z,'pool_search_directory',side_effect=lambda p:z.contextlib.nullcontext('/scoped')), \
             patch.object(z,'pool_guid',return_value=m['pools'][0]['guid']), \
             patch.object(z,'pool_leaves',return_value=['/dev/disk/by-id/wwn-test-part1']), \
             patch.object(z,'run',side_effect=execute):
            with self.assertRaisesRegex(z.Error,'second pool failed'):
                with z.incremental_pools(m,'/dev/disk/by-id/wwn-test'):self.fail('Incomplete pool set yielded')
        self.assertEqual(exports,imports)

    def test_remote_apply_streams_to_local_target_and_failure_skips_boot_updates(self):
        remote=z.RemoteRepository('server','store');remote.dataset='store'
        plan=z.incremental_pool_plan(self.pool,self.m['snapshot'],self.source,self.target)
        plan.update(source=self.source,bytes=4096)
        with patch.object(z,'run'),patch.object(z,'incremental_inventory',return_value=self.target),patch.object(z,'native_dataset_names',return_value=z.native_expected_names(self.pool)), \
             patch.object(z,'pipe_transfer',side_effect=z.Error('receive failed')) as pipe, \
             patch.object(z,'update_incremental_boot') as boot,patch.object(z,'refresh_restored_dracut') as dracut:
            with self.assertRaisesRegex(z.Error,'receive failed'):
                z.apply_incremental_restore(Path('/repo'),self.m,'/dev/disk/by-id/wwn-target',[],
                    [(self.pool,'alias')],{'tank':plan},{},remote)
        sender,receiver=pipe.call_args.args[:2]
        self.assertEqual(sender[0],'ssh');self.assertIn(' -i ',sender[-1])
        self.assertEqual(receiver[0],'zfs');self.assertEqual(receiver[-1],'alias')
        boot.assert_not_called();dracut.assert_not_called()

    def test_bios_restore_changes_only_saved_bytes_inside_existing_partition(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'disk').mkdir()
            (root/'disk'/'first-megabyte.bin').write_bytes(b'A'*z.MIB)
            (root/'disk'/'bios.bin').write_bytes(b'C'*1000)
            disk=root/'disk-device';disk.write_bytes(b'A'*440+b'B'*1000)
            part=root/'partition';part.write_bytes(b'D'*2000)
            layout=[dict(kind='bios_boot',number=1,image='disk/bios.bin')]
            with patch.object(z,'partition_device',return_value=str(part)):
                z.update_incremental_boot(root,str(disk),layout,{})
            self.assertEqual(part.read_bytes(),b'C'*1000+b'D'*1000)
            self.assertEqual(disk.read_bytes(),b'A'*440+b'B'*1000)


class RestoreMountIsolationTests(unittest.TestCase):
    def test_property_replay_never_automounts_parent_or_inherited_children(self):
        # Model the documented libzfs changelist side effect: changing a
        # mountpoint considers descendants, including an unmounted /var/log.
        # Start with canmount=on, as on the already-selected/no-receive path.
        def props(mountpoint,source='local',canmount='on',share='off'):
            return dict(type={'value':'filesystem'},mountpoint={'value':mountpoint,'source':source},
                        canmount={'value':canmount},sharenfs={'value':share},
                        sharesmb={'value':'off'},readonly={'value':'off'})
        manifest={'datasets':{
            'target/var/log':props('/var/log','inherited from target/var'),
            'target/var':props('/var'),
            'target/home':props('/home',share='on'),
            'target/root':props('/',canmount='noauto'),
            'target':props('none',canmount='off'),
            'target/volume':{'type':{'value':'volume'},'volmode':{'value':'default'}},
            'target@point':{'type':{'value':'snapshot'}},
        }}
        for after_receive in (False,True):
            state={n:{k:v['value'] for k,v in p.items()} for n,p in manifest['datasets'].items()}
            if after_receive:
                for p in state.values():
                    if p['type']=='filesystem':p.update(canmount='off',sharenfs='off',sharesmb='off')
            automounted=[]
            def run(*args):
                _,operation,*_,name=args
                self.assertNotIn('@',name)
                if operation=='set':key,value=args[-2].split('=',1)
                else:
                    key=args[-2];value=manifest['datasets'][name][key]['value']
                state[name][key]=value
                if key in ('mountpoint','sharenfs','sharesmb'):
                    for child,p in state.items():
                        if (child==name or child.startswith(name+'/')) and p.get('canmount')=='on':
                            if key=='mountpoint' or p.get('sharenfs','off')!='off' or p.get('sharesmb','off')!='off':
                                automounted.append(child)
                return ''
            with self.subTest(after_receive=after_receive),patch.object(z,'run',side_effect=run) as commands:
                z.restore_mount_properties(manifest)
                self.assertEqual(automounted,[])
                self.assertEqual(state,{n:{k:v['value'] for k,v in p.items()} for n,p in manifest['datasets'].items()})
                self.assertIn(('zfs','inherit','mountpoint','target/var/log'),[c.args for c in commands.call_args_list])

    def test_incomplete_mount_metadata_aborts_before_property_changes(self):
        with patch.object(z,'run') as run,self.assertRaisesRegex(z.Error,'canmount'):
            z.restore_mount_properties({'datasets':{'target/log':{'mountpoint':{'value':'/var/log'}}}})
        run.assert_not_called()

    def test_property_failure_does_not_reenable_mounts(self):
        p={'datasets':{'target':{'mountpoint':{'value':'/var/log'},'canmount':{'value':'on'}}}}
        def run(*args):
            if args[-2]=='mountpoint=/var/log':raise z.Error('property failed')
        with patch.object(z,'run',side_effect=run) as commands,self.assertRaisesRegex(z.Error,'property failed'):
            z.restore_mount_properties(p)
        self.assertNotIn(('zfs','set','canmount=on','target'),[c.args for c in commands.call_args_list])

    def test_entire_restore_dispatches_in_private_namespace_with_live_stdio(self):
        for flags in ([],['--incremental'],['--incremental','--dry-run']):
            argv=['restore',*flags,'--destination','/dev/disk/by-id/wwn-target']
            with self.subTest(flags=flags),patch.object(z.os,'geteuid',return_value=0), \
                 patch.object(z,'commands'),patch.object(z.subprocess,'Popen') as call, \
                 patch.object(z,'restore') as restore:
                call.return_value.wait.return_value=17
                self.assertEqual(z.main(argv),17)
                restore.assert_not_called()
                command=call.call_args.args[0]
                self.assertEqual(command[:4],['unshare','--mount','--propagation','private'])
                self.assertEqual(json.loads(command[-1]),argv)
                self.assertEqual(call.call_args.kwargs,{})
                call.return_value.wait.assert_called_once_with(timeout=1)

    def test_namespace_failure_never_falls_back_to_host_restore(self):
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'commands'), \
             patch.object(z.subprocess,'Popen',side_effect=OSError('unshare failed')), \
             patch.object(z,'restore') as restore,patch('sys.stderr',new_callable=io.StringIO):
            self.assertEqual(z.main(['restore','--incremental']),1)
        restore.assert_not_called()

    def test_namespace_worker_executes_main_once_and_returns_status(self):
        # Execute the actual worker through a harmless stand-in for unshare.
        # The loaded script is a stub, so no root access or disks are involved.
        popen=z.subprocess.Popen
        with tempfile.TemporaryDirectory() as tmp:
            stub=Path(tmp)/'worker.py'
            stub.write_text('def main(argv, *, restore_isolated=False):\n'
                            '    assert restore_isolated\n'
                            '    assert argv == ["restore", "--incremental"]\n'
                            '    return 23\n')
            def launch(command):
                command=command[4:];command[-2]=str(stub)
                return popen(command)
            with patch.object(z,'commands'),patch.object(z.subprocess,'Popen',side_effect=launch):
                self.assertEqual(z.isolated_restore(['restore','--incremental']),23)

    def test_alternate_roots_remain_until_target_pools_are_exported(self):
        manifest=ubuntu_fixture()
        roots={}
        def run(*args):
            if args[:2]==('zpool','list'):return ''
            if args[:2]==('zpool','import'):
                roots[args[-1]]=Path(args[args.index('-R')+1])
                self.assertTrue(roots[args[-1]].is_dir())
            elif args[:2]==('zpool','export'):
                self.assertTrue(roots[args[-1]].is_dir())
        with patch.object(z,'partition_device',return_value='/dev/disk/by-id/test-part2'), \
             patch.object(z,'pool_search_directory',side_effect=lambda p:z.contextlib.nullcontext('/scoped')), \
             patch.object(z,'pool_guid',side_effect=[p['guid'] for p in manifest['pools']]), \
             patch.object(z,'pool_leaves',return_value=['/dev/disk/by-id/test-part2']),patch.object(z,'run',side_effect=run):
            with z.incremental_pools(manifest,'/dev/disk/by-id/test',readonly=False):
                self.assertEqual(len(set(roots.values())),2)
        self.assertTrue(all(not p.exists() for p in roots.values()))


class ClonedRestoreTargetTests(unittest.TestCase):
    disk='/dev/disk/by-id/ata-KINGSTON_TEST'
    guid='5489873998275350292'

    def inspect(self,active=False,busy=False,mounted=False):
        node=dict(path=self.disk,type='disk',children=[dict(path=self.disk+'-part3',type='part',
                  fstype='zfs_member',uuid=self.guid,mountpoints=['/'] if mounted else [])])
        member=self.disk+'-part3' if active else '/dev/disk/by-id/wwn-running-part3'
        status='  pool: rpool\n    '+member+'  ONLINE  0  0  0\n'
        def run(*args):
            if args[:2]==('zpool','status'):return status
            if args[:2]==('zpool','list'):return self.guid+'\n'
            self.fail('Unexpected command: '+repr(args))
        label=z.subprocess.CompletedProcess([],0,('TYPE=zfs_member\nUUID='+self.guid+'\n').encode(),b'')
        with patch.object(z,'node_for',return_value=node),patch.object(z,'stable_device',side_effect=str), \
             patch.object(z.os,'stat',return_value=z.os.stat_result((z.stat.S_IFBLK,)*10)), \
             patch.object(Path,'read_text',return_value='Filename Type Size Used Priority\n'), \
             patch.object(Path,'is_dir',return_value=True),patch.object(Path,'iterdir',side_effect=lambda:iter([])), \
             patch.object(z,'run',side_effect=run),patch.object(z.subprocess,'run',return_value=label), \
             patch.object(z.os,'open',side_effect=OSError(16,'Device or resource busy') if busy else None,return_value=7) as opened, \
             patch.object(z.os,'close') as close:
            result=z.target_idle(self.disk)
        opened.assert_called_once_with(self.disk,z.os.O_RDONLY|z.os.O_EXCL)
        close.assert_called_once_with(7)
        return result

    def test_idle_clone_of_running_pool_is_a_valid_full_restore_target(self):
        self.assertEqual(self.inspect()['path'],self.disk)

    def test_actual_imported_member_is_still_rejected(self):
        with self.assertRaisesRegex(z.Error,'imported pool'):self.inspect(active=True)

    def test_kernel_busy_check_still_rejects_device_with_stale_pool_path(self):
        with self.assertRaisesRegex(OSError,'busy'):self.inspect(busy=True)

    def test_mounted_clone_is_still_rejected(self):
        with self.assertRaisesRegex(z.Error,'mounted'):self.inspect(mounted=True)

    def test_scoped_import_alias_of_target_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            member=Path(tmp)/'member';member.touch()
            alias=Path(tmp)/'scoped-alias';alias.symlink_to(member)
            with patch.object(z,'run',return_value=f'  {alias} ONLINE 0 0 0\n'), \
                 self.assertRaisesRegex(z.Error,'imported pool'):
                z.require_target_not_imported(str(member),[dict(path=str(member))])

    def test_unavailable_destination_is_hidden_before_refresh(self):
        disk=dict(path=self.disk,type='disk',size=64*z.GIB,model='KINGSTON')
        with patch.object(z,'inventory',return_value={'blockdevices':[disk]}), \
             patch.object(z,'target_idle',side_effect=z.Error('Target has active storage layers')), \
             patch('builtins.input',return_value='q'),patch('sys.stdout',new_callable=io.StringIO) as output, \
             self.assertRaises(z.Cancelled):
            z.choose_destination('Restore destination')
        self.assertNotIn(self.disk,output.getvalue())
        self.assertNotIn('Unavailable destinations',output.getvalue())

    def test_only_available_destinations_are_displayed(self):
        disks=[dict(path='/dev/disk/by-id/'+name,type='disk',size=64*z.GIB) for name in
               ('wwn-eligible','wwn-protected','wwn-busy','wwn-readonly','wwn-unidentified')]
        disks[-1]['device_id_error']='Persistent device ID unavailable'
        def idle(path,forbidden):
            if path.endswith('busy'):raise OSError(16,'Device or resource busy')
            if path.endswith('readonly'):raise z.Error('Target is read-only')
        with patch.object(z,'inventory',return_value={'blockdevices':disks}), \
             patch.object(z,'target_idle',side_effect=idle),patch.object(z,'signatures',return_value=[]), \
             patch('builtins.input',return_value='1'),patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(z.choose_destination('Restore',[disks[1]['path']]),disks[0]['path'])
        self.assertIn(disks[0]['path'],output.getvalue())
        for disk in disks[1:]:self.assertNotIn(disk['path'],output.getvalue())
        self.assertNotIn('Unavailable destinations',output.getvalue())


class StandaloneBackupTargetTests(unittest.TestCase):
    def initialize(self,unattended=False,decline=False):
        from unittest.mock import MagicMock
        fake=MagicMock();fake.expanduser.return_value=fake
        fake.exists.return_value=True;fake.stat.return_value.st_mode=z.stat.S_IFBLK
        fake.__str__.return_value='/dev/disk/by-id/ata-KINGSTON_TEST'
        disk=dict(path=str(fake),type='disk',size=64*z.GIB,**{'log-sec':512},children=[
            dict(path=str(fake)+'-part3',type='part',fstype='zfs_member',label='rpool')])
        with patch.object(z,'private_storage_namespace'),patch.object(z,'Path',return_value=fake),patch.object(z,'stable_device',side_effect=str), \
             patch.object(z,'node_for',return_value=disk),patch.object(z,'target_idle',return_value=disk), \
             patch.object(z,'existing_store') as existing,patch.object(z,'print_existing_layout') as layout, \
             patch.object(z,'confirm',side_effect=z.Error('declined') if decline else None) as confirm, \
             patch.object(z,'create_layout') as erase,patch.object(z,'partition_device',return_value=str(fake)+'-part1'), \
             patch.object(z,'recovery_directory',return_value=z.contextlib.nullcontext('/mnt/test')), \
             patch.object(z,'run') as run,patch('sys.stdout',new_callable=io.StringIO):
            if unattended or decline:
                with self.assertRaisesRegex(z.Error,'will not initialize or erase' if unattended else 'declined'):
                    with z.backup_destination(str(fake),'/dev/source',z.GIB,unattended=unattended):
                        self.fail('Unconfirmed initialization')
                erase.assert_not_called();run.assert_not_called()
            else:
                with z.backup_destination(str(fake),'/dev/source',z.GIB):pass
                erase.assert_called_once()
                self.assertEqual([c.args[:2] for c in run.call_args_list],[('zpool','create'),('zpool','export')])
            existing.assert_not_called()
            self.assertEqual(confirm.call_count,0 if unattended else 1)
            self.assertEqual(layout.call_count,0 if unattended else 1)

    def test_standalone_zfs_disk_can_be_initialized_after_confirmation(self):
        self.initialize()

    def test_declining_standalone_initialization_never_erases(self):
        self.initialize(decline=True)

    def test_unattended_never_initializes_standalone_zfs_disk(self):
        self.initialize(unattended=True)

    def test_backup_list_labels_both_types_and_requires_selection(self):
        disks=[dict(path='/dev/backup',type='disk',size=100*z.GIB,children=[
                    dict(fstype='zfs_member',label='linux_os_backup_test')]),
               dict(path='/dev/kingston',type='disk',size=64*z.GIB,children=[
                    dict(fstype='zfs_member',label='rpool')])]
        with patch.object(z,'inventory',return_value={'blockdevices':disks}), \
             patch.object(z,'target_idle'),patch('builtins.input',return_value='2') as prompt, \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(z.choose_destination('Backup destination',allow_path=True),'/dev/kingston')
        prompt.assert_called_once()
        self.assertIn('Available backup targets:',output.getvalue())
        self.assertIn('Existing backup disk',output.getvalue())
        self.assertIn('Other disk; contains data',output.getvalue())
        self.assertNotIn('Unavailable destinations',output.getvalue())


class TransferFailureCleanupTests(unittest.TestCase):
    def setUp(self):
        self.children=[]
        self.popen=z.subprocess.Popen
        for name,value in (('DIAGNOSTIC',False),('UNREAPED_TRANSFERS',[]),
                           ('TRANSFER_EOF_TIMEOUT',.1),('TRANSFER_TERM_TIMEOUT',.1),
                           ('TRANSFER_KILL_TIMEOUT',.2)):
            patcher=patch.object(z,name,value);patcher.start();self.addCleanup(patcher.stop)
        self.addCleanup(self.reap)

    def reap(self):
        for p in self.children:
            if p.poll() is None:
                z.os.killpg(p.pid,z.signal.SIGKILL)
                p.wait(timeout=2)
            for stream in (p.stdin,p.stdout,p.stderr):
                if stream is not None and not stream.closed:stream.close()

    def spawn(self,*args,**kwargs):
        child=self.popen(*args,**kwargs);self.children.append(child)
        return child

    def transfer(self,sender,receiver,**kwargs):
        with patch.object(z.subprocess,'Popen',side_effect=self.spawn), \
             patch('sys.stdout',new_callable=io.StringIO),patch('sys.stderr',new_callable=io.StringIO):
            return z.pipe_transfer([sys.executable,'-c',sender],[sys.executable,'-c',receiver],None,'test',**kwargs)

    def test_receiver_enospc_stops_sender_and_preserves_error(self):
        with self.assertRaisesRegex(z.Error,'No space left on device'):
            self.transfer("import sys\nwhile True: sys.stdout.buffer.write(b'x'*1048576)",
                          "import sys;sys.stdin.buffer.read(4096);sys.stderr.write('No space left on device\\n');sys.exit(1)")
        self.assertEqual(len(self.children),2)
        self.assertTrue(all(p.poll() is not None for p in self.children))

    def test_receiver_failure_detected_even_if_sender_produces_nothing(self):
        started=z.time.monotonic()
        with self.assertRaisesRegex(z.Error,'No space left on device'):
            self.transfer('import time;time.sleep(60)',
                          "import sys;sys.stderr.write('No space left on device');sys.exit(1)")
        self.assertLess(z.time.monotonic()-started,5)
        self.assertTrue(all(p.poll() is not None for p in self.children))

    def test_receiver_failure_detected_after_sender_closes_stdout_but_stays_alive(self):
        with self.assertRaisesRegex(z.Error,'receiver failed'):
            self.transfer('import os,time;os.close(1);time.sleep(60)',
                          "import sys;sys.stdin.buffer.read();sys.stderr.write('receiver failed');sys.exit(1)")
        self.assertTrue(all(p.poll() is not None for p in self.children))

    def test_receiver_start_failure_reaps_sender(self):
        def spawn(command,**kwargs):
            if self.children:raise FileNotFoundError('receiver missing')
            return self.spawn(command,**kwargs)
        with patch.object(z.subprocess,'Popen',side_effect=spawn), \
             patch('sys.stdout',new_callable=io.StringIO),self.assertRaisesRegex(z.Error,'receiver missing'):
            z.pipe_transfer([sys.executable,'-c','import time;time.sleep(60)'],['missing'],None,'test')
        self.assertIsNotNone(self.children[0].poll())

    def test_broken_pipe_during_close_does_not_skip_cleanup_or_mask_original_error(self):
        class BrokenClose:
            def __init__(self,stream):self.stream=stream
            def fileno(self):return self.stream.fileno()
            @property
            def closed(self):return self.stream.closed
            def close(self):
                self.stream.close()
                raise BrokenPipeError('close failed')
        def spawn(command,**kwargs):
            self.assertEqual(kwargs['bufsize'],0)
            child=self.spawn(command,**kwargs)
            if child.stdin is not None:child.stdin=BrokenClose(child.stdin)
            return child
        def fail(count):raise RuntimeError('original failure')
        with patch.object(z.subprocess,'Popen',side_effect=spawn), \
             patch('sys.stdout',new_callable=io.StringIO),patch('sys.stderr',new_callable=io.StringIO):
            with self.assertRaisesRegex(RuntimeError,'original failure'):
                z.pipe_transfer([sys.executable,'-c',"import sys;sys.stdout.buffer.write(b'x'*1048576)"],
                    [sys.executable,'-c','import sys;sys.stdin.buffer.read()'],None,'test',on_bytes=fail)
        self.assertTrue(all(p.poll() is not None for p in self.children))

    def test_partial_writes_and_eagain_preserve_all_bytes(self):
        write=z.os.write;read=z.os.read;writes=0;reads=0;counts=[]
        def partial(fd,data):
            nonlocal writes
            writes+=1
            if writes==1:raise BlockingIOError('try again')
            return write(fd,data[:257])
        def read_again(fd,size):
            nonlocal reads
            reads+=1
            if reads==1:raise BlockingIOError('try again')
            return read(fd,size)
        def spawn(*args,**kwargs):
            child=self.spawn(*args,**kwargs)
            if child.stdin is not None:
                patcher=patch.object(z.os,'read',side_effect=read_again)
                patcher.start();self.addCleanup(patcher.stop)
            return child
        with patch.object(z.subprocess,'Popen',side_effect=spawn),patch.object(z.os,'write',side_effect=partial), \
             patch('sys.stdout',new_callable=io.StringIO):
            size=z.pipe_transfer([sys.executable,'-c',"import sys;sys.stdout.buffer.write(bytes(range(256))*1000)"],
                [sys.executable,'-c',"import sys;assert sys.stdin.buffer.read()==bytes(range(256))*1000"],
                None,'test',on_bytes=counts.append)
        self.assertEqual(size,256000);self.assertEqual(sum(counts),size)
        self.assertGreater(writes,1000)

    def test_child_ignoring_term_is_killed_and_reaped(self):
        def fail(count):raise RuntimeError('stop transfer')
        with self.assertRaisesRegex(RuntimeError,'stop transfer'):
            self.transfer("import sys,time;time.sleep(.2);sys.stdout.buffer.write(b'x'*1048576)",
                'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)',on_bytes=fail)
        self.assertEqual(self.children[1].returncode,-z.signal.SIGKILL)
        self.assertTrue(all(p.poll() is not None for p in self.children))

    def test_unreapable_child_never_gets_unbounded_wait_or_followup_pool_commands(self):
        from unittest.mock import Mock
        process=Mock(pid=12345)
        process.poll.return_value=None
        process.wait.side_effect=z.subprocess.TimeoutExpired('receive',1)
        with patch.object(z.os,'killpg') as kill,patch('sys.stderr',new_callable=io.StringIO) as output:
            z.stop_transfer_processes([process],process)
        self.assertEqual([c.args[1] for c in kill.call_args_list],[z.signal.SIGTERM,z.signal.SIGKILL])
        self.assertTrue(all(c.kwargs.get('timeout') is not None for c in process.wait.call_args_list))
        self.assertIn('has not exited after SIGKILL',output.getvalue())
        with patch.object(z.subprocess,'run') as run:
            for command in (('zpool','export','target'),('zpool','list'),('zfs','set','x=y','target'),('umount','/tmp/target')):
                with self.assertRaisesRegex(z.Error,'leaving pools imported'):z.run(*command)
        run.assert_not_called()

    def test_cleanup_exception_still_blocks_export_until_receiver_exits(self):
        from unittest.mock import Mock
        process=Mock(pid=12345)
        process.poll.return_value=None
        process.wait.side_effect=KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):z.stop_transfer_processes([process],process)
        with patch.object(z.subprocess,'run') as run:
            with self.assertRaisesRegex(z.Error,'leaving pools imported'):
                z.run('zpool','export','target')
            run.assert_not_called()
        process.poll.return_value=0
        with patch.object(z.subprocess,'run',return_value=z.subprocess.CompletedProcess([],0,b'',b'')) as run:
            z.run('zpool','export','target')
            run.assert_called_once()

    def test_diagnostics_log_stalled_receiver_and_command_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(z.Path,'cwd',return_value=Path(tmp)), \
             patch.object(z,'DIAGNOSTIC',True),patch.object(z,'DIAGNOSTIC_INTERVAL',.03), \
             patch('sys.stderr',new_callable=io.StringIO):
            with z.diagnostic_session():
                self.transfer("import sys;sys.stdout.buffer.write(b'x'*1048576)",
                    'import sys,time;time.sleep(.2);sys.stdin.buffer.read();time.sleep(.2)')
                z.run(sys.executable,'-c','import time;time.sleep(.1)')
            logs=list(Path(tmp).glob('lllzorb-diagnostic-*.log'))
            self.assertEqual(len(logs),1)
            log=logs[0].read_text()
            self.assertEqual(logs[0].stat().st_mode&0o777,0o600)
        for expected in ('stage=writing receiver','stage=waiting for receiver exit','no progress for',
                         'receiver exit=None; pid=','wchan=','cleanup finished','command start:',
                         'command still running','command child:','command exit=0','command finished',
                         'pipeline starting','pipeline finished'):
            self.assertIn(expected,log)

    def test_diagnostic_output_failure_does_not_raise(self):
        from unittest.mock import Mock
        bad=Mock();bad.write.side_effect=OSError('log full')
        with patch.object(z,'DIAGNOSTIC',True),patch.object(z,'DIAGNOSTIC_LOG',bad),patch('sys.stderr',bad):
            z.diagnostic_event('still clean up')

    def test_iostat_monitor_ignoring_term_is_reaped_without_a_pipe_reader(self):
        def spawn(command,**kwargs):
            if command[:2]==['zfs','send']:
                script="import sys;sys.stdout.buffer.write(b'x'*100000)"
            elif command[:2]==['zfs','receive']:
                script='import sys,time;sys.stdin.buffer.read();time.sleep(.2)'
            else:
                self.assertEqual(command,['zpool','iostat','-Hp','source','target','5'])
                self.assertNotEqual(kwargs['stdout'],z.subprocess.PIPE)
                script=("import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                        "print('target allocation sample',flush=True);time.sleep(60)")
            return self.spawn([sys.executable,'-c',script],**kwargs)
        with patch.object(z,'DIAGNOSTIC',True),patch.object(z,'DIAGNOSTIC_INTERVAL',.03), \
             patch.object(z.subprocess,'Popen',side_effect=spawn),patch.object(z.shutil,'which',return_value='/sbin/zpool'), \
             patch('sys.stdout',new_callable=io.StringIO),patch('sys.stderr',new_callable=io.StringIO) as output:
            self.assertEqual(z.pipe_transfer(['zfs','send','source/data@s'],['zfs','receive','target/data'],
                                            None,'test'),100000)
        self.assertIn('target allocation sample',output.getvalue())
        self.assertEqual(self.children[-1].returncode,-z.signal.SIGKILL)
        self.assertTrue(all(p.poll() is not None for p in self.children))

    def test_flag_opens_log_and_keeps_original_operation_error(self):
        for command in ('backup','restore'):
            with self.subTest(command=command),tempfile.TemporaryDirectory() as tmp,patch.object(z.Path,'cwd',return_value=Path(tmp)), \
                 patch.object(z.os,'geteuid',return_value=0),patch.object(z,command,side_effect=z.Error('test failure')), \
                 patch('sys.stderr',new_callable=io.StringIO):
                self.assertEqual(z.main([command,'--diagnostic'],restore_isolated=True),1)
                logs=list(Path(tmp).glob('lllzorb-diagnostic-*.log'))
                self.assertEqual(len(logs),1)
                self.assertIn('operation failed: Error: test failure',logs[0].read_text())
            self.assertIsNone(z.DIAGNOSTIC_LOG)


class SnapshotDatasetSizeTests(unittest.TestCase):
    def pool(self):
        return dict(name='rpool',datasets={
            'rpool':{'referenced':{'value':'999999999999'}},'rpool/child':{},
            'rpool@selected':{'referenced':{'value':str(z.GIB)},'logicalreferenced':{'value':str(3*z.GIB)}},
            'rpool/child@selected':{'referenced':{'value':str(2*z.GIB)},'logicalreferenced':{'value':str(4*z.GIB)}},
            'rpool@old':{'referenced':{'value':'888888888888'}},
        })

    def test_saved_sizes_use_selected_snapshot_and_sum_children_once(self):
        self.assertEqual(z.saved_snapshot_sizes([self.pool()],'selected'),
                         dict(compressed=3*z.GIB,uncompressed=7*z.GIB))

    def test_missing_snapshot_never_uses_live_values_or_displays_partial_total(self):
        pool=self.pool();del pool['datasets']['rpool/child@selected']
        self.assertEqual(z.saved_snapshot_sizes([pool],'selected'),dict(compressed=None,uncompressed=None))
        self.assertIn('compressed unavailable',z.dataset_size_text(z.saved_snapshot_sizes([pool],'selected')))

    def test_bad_property_is_unavailable_but_zero_is_valid(self):
        props=[{'referenced':{'value':'0'},'logicalreferenced':{'value':'-'}}]
        self.assertEqual(z.dataset_sizes(props),dict(compressed=0,uncompressed=None))
        self.assertEqual(z.dataset_size_text(z.dataset_sizes(props)),
                         'datasets: compressed 0.000 GiB, uncompressed unavailable')

    def test_live_sizes_count_each_snapshot_once_and_do_not_use_unique_used_bytes(self):
        with patch.object(z,'run',return_value='rpool@s\t100\t400\nrpool/child@s\t200\t600\n') as run:
            result=z.snapshot_dataset_sizes(['rpool@s','rpool/child@s','rpool@s'])
        self.assertEqual(result,dict(compressed=300,uncompressed=1000))
        run.assert_called_once_with('zfs','list','-H','-p','-t','snapshot','-o',
                                    'name,referenced,logicalreferenced','rpool/child@s','rpool@s')

    def test_failed_or_incomplete_query_does_not_display_a_partial_total(self):
        for output in ('rpool@s\t100\t400\n','invalid','other@s\t100\t400\n'):
            with patch.object(z,'run',return_value=output):
                self.assertEqual(z.snapshot_dataset_sizes(['rpool@s','rpool/child@s']),
                                 dict(compressed=None,uncompressed=None))
        with patch.object(z,'run',side_effect=z.Error('unavailable')):
            self.assertEqual(z.snapshot_dataset_sizes(['rpool@s']),dict(compressed=None,uncompressed=None))

    def test_restore_selection_shows_saved_sizes_alongside_stream_estimate(self):
        m=native_fixture();m['pools']=[self.pool()]
        m['pools'][0]['datasets']={n.replace('@selected','@'+m['snapshot']):p
                                  for n,p in m['pools'][0]['datasets'].items()}
        with patch.object(z,'select_host_repository',return_value=Path('/repo')), \
             patch.object(z,'catalog',return_value=[(Path('/repo/a'),m)]), \
             patch.object(z,'estimate_native_send',return_value=8*z.GIB), \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(z.select_backup('/repo',show_sizes=True),Path('/repo/a'))
        self.assertIn('Saved snapshot datasets: compressed 3.000 GiB, uncompressed 7.000 GiB',output.getvalue())
        self.assertIn('ZFS restore: ~8.000 GiB',output.getvalue())


class RestoreLauncherInterruptTests(unittest.TestCase):
    def check_interrupt(self,parent_only=False,repeat=False,during_spawn=False,transfer=False,backup=False):
        # Run the real launcher and worker main with restore replaced by harmless cleanup.
        program=str(Path(z.__file__).resolve())
        with tempfile.TemporaryDirectory(prefix='zfs-launcher-test-') as tmp:
            root=Path(tmp);stub=root/'worker.py'
            stub.write_text(
                'import os,runpy,time\nfrom pathlib import Path\n'
                f'program=runpy.run_path({program!r})\nroot=Path({tmp!r})\n'
                'def restore(args):\n'
                '    try:\n'
                '        (root/"ready").write_text(str(os.getpid()))\n'
                '        time.sleep(30)\n'
                '    finally:\n'
                '        (root/"cleanup-started").touch()\n'
                '        time.sleep(.7)\n'
                '        (root/"cleanup-finished").touch()\n'
                'main=program["main"]\n'
                'main.__globals__["restore"]=restore\n'
                'program["os"].geteuid=lambda:0\n')
            if transfer:
                sender="import sys\nwhile True:sys.stdout.buffer.write(b'x'*65536)"
                receiver='import sys,time\nwhile sys.stdin.buffer.read(65536):time.sleep(.01)'
                if backup:
                    receiver+=f'\nfrom pathlib import Path\nPath({str(root/"receiver-cleanup")!r}).touch()\ntime.sleep(.7)'
                workload=(
                    '        children=[];original=program["subprocess"].Popen\n'
                    '        def launch(*args,**kwargs):\n'
                    '            child=original(*args,**kwargs);children.append(child)\n'
                    '            (root/"children").write_text(" ".join(str(p.pid) for p in children))\n'
                    '            return child\n'
                    '        program["subprocess"].Popen=launch\n'
                    '        def ready(count):\n'
                    '            if not (root/"ready").exists():(root/"ready").write_text(str(os.getpid()))\n'
                    f'        program["pipe_transfer"]([{sys.executable!r},"-c",{sender!r}],'
                    f'[{sys.executable!r},"-c",{receiver!r}],None,"test",on_bytes=ready)\n')
                text=stub.read_text().replace('        (root/"ready").write_text(str(os.getpid()))\n'
                                             '        time.sleep(30)\n',workload)
                text=text.replace('        (root/"cleanup-started").touch()\n',
                    '        assert all(p.poll() is not None for p in children)\n'
                    '        (root/"cleanup-started").touch()\n')
                stub.write_text(text)
            if backup:
                stub.write_text(stub.read_text().replace('def restore(args):','def backup(args):')
                                .replace('["restore"]=restore','["backup"]=backup'))
            parent=(
                'import runpy,subprocess,sys,time\nfrom pathlib import Path\n'
                'program=runpy.run_path(sys.argv[1]);original=subprocess.Popen\n'
                'def launch(command):\n'
                '    command=command[4:];command[-2]=sys.argv[2]\n'
                '    child=original(command)\n'
                '    if sys.argv[3]=="delay":\n'
                '        while not Path(sys.argv[2]).with_name("ready").exists():time.sleep(.01)\n'
                '        Path(sys.argv[2]).with_name("spawning").touch()\n'
                '        time.sleep(.3)\n'
                '    return child\n'
                'subprocess.Popen=launch\n'
                'sys.exit(program["isolated_restore"](["restore"]))\n')
            if backup:
                parent='import runpy,sys\nworker=runpy.run_path(sys.argv[2]);sys.exit(worker["main"](["backup"]))\n'
            parent_process=z.subprocess.Popen([sys.executable,'-c',parent,program,str(stub),
                                               'delay' if during_spawn else 'normal'],
                start_new_session=True,stdout=z.subprocess.PIPE,stderr=z.subprocess.PIPE,text=True)
            def wait_for(name):
                deadline=z.time.monotonic()+5
                while not (root/name).exists() and parent_process.poll() is None and z.time.monotonic()<deadline:
                    z.time.sleep(.01)
                self.assertTrue((root/name).exists(),f'Missing {name} marker')
            try:
                wait_for('spawning' if during_spawn else 'ready')
                if parent_only:z.os.kill(parent_process.pid,z.signal.SIGINT)
                else:z.os.killpg(parent_process.pid,z.signal.SIGINT)
                wait_for('receiver-cleanup' if backup and transfer else 'cleanup-started')
                if repeat:
                    for _ in range(3):
                        z.os.killpg(parent_process.pid,z.signal.SIGINT)
                        z.time.sleep(.03)
                stdout,stderr=parent_process.communicate(timeout=5)
                self.assertEqual(parent_process.returncode,130,stderr)
                self.assertTrue((root/'cleanup-finished').exists(),stderr)
                self.assertEqual(stderr.count('Interrupted; any backup snapshots are retained.'),1)
                self.assertNotIn('Traceback',stderr)
                worker_pid=int((root/'ready').read_text())
                self.assertFalse(Path(f'/proc/{worker_pid}').exists())
                if transfer:
                    for pid in (root/'children').read_text().split():
                        self.assertFalse(Path(f'/proc/{pid}').exists())
            finally:
                with z.contextlib.suppress(ProcessLookupError):
                    z.os.killpg(parent_process.pid,z.signal.SIGKILL)
                if (root/'children').exists():
                    for pid in (root/'children').read_text().split():
                        with z.contextlib.suppress(ProcessLookupError):z.os.killpg(int(pid),z.signal.SIGKILL)
                parent_process.communicate(timeout=5)

    def test_terminal_ctrl_c_waits_for_worker_cleanup(self):self.check_interrupt()

    def test_parent_only_sigint_is_forwarded(self):self.check_interrupt(parent_only=True)

    def test_repeated_ctrl_c_does_not_interrupt_cleanup(self):self.check_interrupt(repeat=True)

    def test_backup_repeated_ctrl_c_waits_for_receiver_and_outer_cleanup(self):
        self.check_interrupt(backup=True,transfer=True,repeat=True)

    def test_backup_single_ctrl_c_finishes_cleanup(self):self.check_interrupt(backup=True)

    def test_transfer_children_exit_before_worker_cleanup_completes(self):
        self.check_interrupt(transfer=True,repeat=True)

    def test_sigint_during_spawn_is_forwarded_after_worker_is_assigned(self):
        self.check_interrupt(parent_only=True,during_spawn=True)

    def test_launcher_restores_handler_after_success_and_spawn_failure(self):
        handler=z.signal.getsignal(z.signal.SIGINT)
        with patch.object(z,'commands'),patch.object(z.subprocess,'Popen') as popen:
            popen.return_value.wait.return_value=0
            self.assertEqual(z.isolated_restore(['restore']),0)
        self.assertIs(z.signal.getsignal(z.signal.SIGINT),handler)
        with patch.object(z,'commands'),patch.object(z.subprocess,'Popen',side_effect=OSError('spawn failed')):
            with self.assertRaisesRegex(OSError,'spawn failed'):z.isolated_restore(['restore'])
        self.assertIs(z.signal.getsignal(z.signal.SIGINT),handler)

    def test_operation_handler_is_used_for_backup_and_restored_afterwards(self):
        handler=z.signal.getsignal(z.signal.SIGINT)
        with self.assertRaises(KeyboardInterrupt):
            with z.operation_interrupts():
                interrupt=z.signal.getsignal(z.signal.SIGINT)
                self.assertIsNot(interrupt,handler)
                try:interrupt(z.signal.SIGINT,None)
                finally:self.assertEqual(z.signal.getsignal(z.signal.SIGINT),z.signal.SIG_IGN)
        self.assertIs(z.signal.getsignal(z.signal.SIGINT),handler)
        def backup(args):self.assertIsNot(z.signal.getsignal(z.signal.SIGINT),handler)
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'backup',side_effect=backup):
            self.assertEqual(z.main(['backup']),0)
        self.assertIs(z.signal.getsignal(z.signal.SIGINT),handler)

    def test_worker_signal_exit_code_is_preserved(self):
        with patch.object(z,'commands'),patch.object(z.subprocess,'Popen') as popen:
            popen.return_value.wait.return_value=-z.signal.SIGTERM
            self.assertEqual(z.isolated_restore(['restore']),143)


class ConcurrentOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='zfs-concurrent-test-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.locks=self.root/'locks'
        self.patch=patch.object(z,'LOCK_DIRECTORY',self.locks)
        self.patch.start();self.addCleanup(self.patch.stop)
        self.aliases={}
        ids=self.root/'by-id';ids.mkdir()
        for disk in ('one','two'):
            raw=self.root/disk;raw.touch()
            names=[]
            for prefix in ('wwn-','ata-'):
                alias=ids/(prefix+disk);alias.symlink_to(raw);names.append(str(alias))
            self.aliases[str(raw)]=names
        self.ids=ids
        for item in (patch.object(z,'DISK_IDS',ids),patch.object(z,'device_ids',return_value=self.aliases)):
            item.start();self.addCleanup(item.stop)
        self.prefix=(
            'import contextlib,json,os,runpy,sys,time\nfrom pathlib import Path\n'
            f'z=runpy.run_path({str(Path(z.__file__).resolve())!r})\n'
            f'z["local_lock"].__globals__["LOCK_DIRECTORY"]=Path({str(self.locks)!r})\n'
            f'z["stable_device"].__globals__["DISK_IDS"]=Path({str(ids)!r})\n'
            f'z["stable_device"].__globals__["device_ids"]=lambda:{self.aliases!r}\n')

    def child(self,code):
        process=z.subprocess.Popen([sys.executable,'-c',self.prefix+code],stdin=z.subprocess.PIPE,
                                   stdout=z.subprocess.PIPE,stderr=z.subprocess.PIPE,text=True,start_new_session=True)
        def cleanup():
            with z.contextlib.suppress(ProcessLookupError):z.os.killpg(process.pid,z.signal.SIGKILL)
            process.communicate(timeout=5)
        self.addCleanup(cleanup)
        return process

    def probe(self,expression):
        process=self.child('try:\n    with '+expression+':pass\n'
                           'except z["LockBusy"] as e:\n    print(e);sys.exit(1)\n')
        out,err=process.communicate(timeout=5)
        self.assertIn(process.returncode,(0,1),err)
        return process.returncode,out

    def test_restore_aliases_conflict_but_other_disk_remains_available(self):
        with z.device_lock(self.ids/'ata-one'):
            status,message=self.probe(f'z["device_lock"]({str(self.ids/"wwn-one")!r})')
            self.assertEqual(status,1);self.assertIn('in use by another operation',message)
            self.assertEqual(self.probe(f'z["device_lock"]({str(self.ids/"ata-two")!r})')[0],0)
        self.assertEqual(self.probe(f'z["device_lock"]({str(self.ids/"wwn-one")!r})')[0],0)

    def test_one_backup_writer_per_disk_allows_readers_and_other_disks(self):
        with z.backup_writer_lock(self.ids/'ata-one'),z.device_lock(self.ids/'ata-one',shared=True):
            self.assertEqual(self.probe(f'z["backup_writer_lock"]({str(self.ids/"wwn-one")!r})')[0],1)
            self.assertEqual(self.probe(f'z["backup_writer_lock"]({str(self.ids/"ata-two")!r})')[0],0)
            self.assertEqual(self.probe(f'z["device_lock"]({str(self.ids/"wwn-one")!r},shared=True)')[0],0)
            self.assertEqual(self.probe(f'z["device_lock"]({str(self.ids/"wwn-one")!r})')[0],1)

    def test_explicit_restore_lock_precedes_storage_and_survives_cleanup(self):
        target=str(self.ids/'ata-one')
        def restore(args):
            self.assertEqual(self.probe(f'z["device_lock"]({target!r})')[0],1)
            try:raise KeyboardInterrupt
            finally:self.assertEqual(self.probe(f'z["device_lock"]({target!r})')[0],1)
        with patch.object(z,'restore_locked',side_effect=restore),self.assertRaises(KeyboardInterrupt):
            z.restore(z.argparse.Namespace(target=target))
        self.assertEqual(self.probe(f'z["device_lock"]({target!r})')[0],0)

    def test_interactive_target_lock_outlives_inner_restore_and_storage_cleanup(self):
        target=str(self.ids/'ata-one')
        def restore(args):
            with z.restore_target_lock(target):pass
            self.assertEqual(self.probe(f'z["device_lock"]({target!r})')[0],1)
        with patch.object(z,'restore_locked',side_effect=restore):z.restore(z.argparse.Namespace(target=None))
        self.assertEqual(self.probe(f'z["device_lock"]({target!r})')[0],0)

    def test_busy_restore_target_returns_error_before_storage_or_prompts(self):
        target=str(self.ids/'ata-one')
        owner=self.child(f'with z["device_lock"]({target!r}):\n    print("ready",flush=True)\n    sys.stdin.read()\n')
        ready,_,_=z.select.select([owner.stdout],[],[],5);self.assertTrue(ready)
        self.assertEqual(owner.stdout.readline().strip(),'ready')
        with patch.object(z.os,'geteuid',return_value=0),patch.object(z,'lock_directory',return_value=self.locks),patch.object(z,'restore_locked') as restore, \
             patch('builtins.input') as prompt,patch('sys.stderr',new_callable=io.StringIO) as errors:
            self.assertEqual(z.main(['restore','--destination',target],restore_isolated=True),1)
        restore.assert_not_called();prompt.assert_not_called()
        self.assertIn('in use by another operation',errors.getvalue())

    def test_busy_backup_writer_fails_before_opening_storage(self):
        target=str(self.ids/'ata-one')
        owner=self.child(f'with z["backup_writer_lock"]({target!r}):\n    print("ready",flush=True)\n    sys.stdin.read()\n')
        ready,_,_=z.select.select([owner.stdout],[],[],5);self.assertTrue(ready)
        self.assertEqual(owner.stdout.readline().strip(),'ready')
        with patch.object(z.stat,'S_ISBLK',return_value=True),patch.object(z,'backup_destination_locked') as storage:
            with self.assertRaises(z.LockBusy),z.backup_destination(target,None,1):pass
        storage.assert_not_called()

    def test_crashed_parent_does_not_release_a_live_transfer_childs_lock(self):
        code=(
            'with z["local_lock"]("survivor"):\n'
            '    child=z["locked_popen"]([sys.executable,"-c","import time;time.sleep(30)"],'
            'stdout=-3,stderr=-3,start_new_session=True)\n'
            '    print(child.pid,flush=True)\n'
            '    sys.stdin.read()\n')
        process=self.child(code)
        ready,_,_=z.select.select([process.stdout],[],[],5);self.assertTrue(ready)
        child_pid=int(process.stdout.readline())
        try:
            process.kill();process.communicate(timeout=5)
            self.assertEqual(self.probe('z["local_lock"]("survivor")')[0],1)
            z.os.kill(child_pid,z.signal.SIGKILL)
            deadline=z.time.monotonic()+5
            while self.probe('z["local_lock"]("survivor")')[0] and z.time.monotonic()<deadline:
                z.time.sleep(.02)
            self.assertEqual(self.probe('z["local_lock"]("survivor")')[0],0)
        finally:
            with z.contextlib.suppress(ProcessLookupError):z.os.kill(child_pid,z.signal.SIGKILL)

    def test_host_readers_protect_snapshots_while_other_hosts_can_be_backed_up(self):
        repo=self.root/'repository';repo.mkdir()
        one=repo/'hosts'/'one';one.mkdir(parents=True)
        two=repo/'hosts'/'two';two.mkdir()
        with patch.object(z,'select_host_repository',return_value=one),z.repository_reader(repo):
            self.assertEqual(self.probe(f'z["repository_lock"]({str(one)!r},shared=True,name=".host.lock")')[0],0)
            self.assertEqual(self.probe(f'z["repository_lock"]({str(one)!r},name=".host.lock")')[0],1)
            self.assertEqual(self.probe(f'z["repository_lock"]({str(two)!r},name=".host.lock")')[0],0)
            self.assertEqual(self.probe(f'z["repository_lock"]({str(repo)!r},name=".readers.lock")')[0],1)
            self.assertEqual(self.probe(f'z["repository_lock"]({str(repo)!r})')[0],0)

    def test_shared_pool_is_exported_only_by_the_last_process(self):
        imported=self.root/'imported';events=self.root/'events'
        backend=(
            f'imported=Path({str(imported)!r});events=Path({str(events)!r})\n'
            'def run(*a,**kw):\n'
            '    if a[:2]==("zpool","list"):return "linux_os_backup_test\\n" if imported.exists() else ""\n'
            '    if a[:2]==("zpool","export"):\n'
            '        imported.unlink()\n'
            '        with events.open("a") as f:f.write("export\\n")\n'
            '        return ""\n'
            '    raise AssertionError(a)\n'
            'def acquire(*a):\n'
            '    time.sleep(.1)\n'
            '    imported.touch()\n'
            '    with events.open("a") as f:f.write("import\\n")\n'
            'g=z["backup_pool_usage"].__wrapped__.__globals__\n'
            'g.update(run=run,import_backup_pool=acquire,target_idle=lambda *a:None,'
            'check_import_discovery=lambda *a:None,pool_leaves=lambda *a:["/dev/member"],'
            'props=lambda *a:{"linux_os_backup_test":{"guid":{"value":"123"}}})\n'
            'with z["backup_pool_usage"]("/dev/disk","/dev/member","linux_os_backup_test","123",None):\n'
            '    print("ready",flush=True)\n'
            '    sys.stdin.read()\n')
        processes=[self.child(backend) for _ in range(3)]
        for process in processes:
            ready,_,_=z.select.select([process.stdout],[],[],5);self.assertTrue(ready)
            message=process.stdout.readline().strip()
            self.assertEqual(message,'ready',process.communicate(timeout=5)[1] if not message else '')
        self.assertEqual(events.read_text(),'import\n')
        for process in processes[:-1]:
            out,err=process.communicate('',timeout=5);self.assertEqual(process.returncode,0,err)
            self.assertTrue(imported.exists());self.assertEqual(events.read_text(),'import\n')
        out,err=processes[-1].communicate('',timeout=5)
        self.assertEqual(processes[-1].returncode,0,err)
        self.assertFalse(imported.exists());self.assertEqual(events.read_text(),'import\nexport\n')
        self.assertFalse((self.locks/'pool-123.json').exists())


class BackupPoolLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='zfs-pool-lease-test-')
        self.addCleanup(self.temp.cleanup)
        patcher=patch.object(z,'LOCK_DIRECTORY',Path(self.temp.name));patcher.start();self.addCleanup(patcher.stop)
        self.imported=False;self.events=[];self.name='linux_os_backup_test'
        self.stack=z.contextlib.ExitStack();self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(z,'run',side_effect=self.execute))
        self.stack.enter_context(patch.object(z,'props',return_value={self.name:{'guid':{'value':'123'}}}))
        self.stack.enter_context(patch.object(z,'pool_leaves',return_value=['/dev/member']))
        self.stack.enter_context(patch.object(z,'target_idle'))
        self.stack.enter_context(patch.object(z,'check_import_discovery'))
        self.stack.enter_context(patch.object(z,'import_backup_pool',side_effect=self.acquire))

    def execute(self,*args,**kwargs):
        if args[:2]==('zpool','list'):return self.name+'\n' if self.imported else ''
        if args[:2]==('zpool','export'):
            self.events.append('export');self.imported=False;return ''
        self.fail('Unexpected command '+repr(args))

    def acquire(self,*args):
        self.events.append('import');self.imported=True

    def usage(self):return z.backup_pool_usage('/dev/disk','/dev/member',self.name,'123',None)

    def test_preexisting_import_is_preserved(self):
        self.imported=True
        with self.usage():pass
        self.assertTrue(self.imported);self.assertEqual(self.events,[])

    def test_failed_user_does_not_export_another_users_pool(self):
        with self.usage():
            with self.assertRaisesRegex(z.Error,'failure'),self.usage():raise z.Error('failure')
            self.assertTrue(self.imported);self.assertEqual(self.events,['import'])
        self.assertFalse(self.imported);self.assertEqual(self.events,['import','export'])

    def test_import_failure_cleans_pending_ownership_and_preserves_error(self):
        with patch.object(z,'import_backup_pool',side_effect=z.Error('import failed')):
            with self.assertRaisesRegex(z.Error,'import failed'),self.usage():pass
        self.assertFalse((Path(self.temp.name)/'pool-123.json').exists())
        self.assertEqual(self.events,[])

    def test_crash_record_is_adopted_and_last_user_exports(self):
        self.imported=True
        root=Path(self.temp.name)
        (root/'pool-123-devices').mkdir()
        (root/'pool-123.json').write_text(json.dumps(dict(name=self.name,guid='123',member='/dev/member')))
        with self.usage():pass
        self.assertFalse(self.imported);self.assertEqual(self.events,['export'])

    def test_export_failure_retains_ownership_for_retry(self):
        original=self.execute
        def fail(*args,**kwargs):
            if args[:2]==('zpool','export'):raise z.Error('busy')
            return original(*args,**kwargs)
        with patch.object(z,'run',side_effect=fail),self.assertRaisesRegex(z.Error,'busy'),self.usage():pass
        self.assertTrue((Path(self.temp.name)/'pool-123.json').exists())
        self.assertTrue(self.imported)
        with self.usage():pass
        self.assertFalse(self.imported)

    def test_orphan_cleanup_only_exports_pools_on_selected_device(self):
        one='restore_'+'1'*16;two='restore_'+'2'*16
        rows=f'{one}\t/tmp/lllzorb-target-one\tONLINE\n{two}\t/tmp/lllzorb-target-two\tSUSPENDED\n'
        leaves={one:['/dev/one1'],two:['/dev/two1']}
        with patch.object(z,'node_for',return_value=dict(path='/dev/one',children=[dict(path='/dev/one1')],type='disk')), \
             patch.object(z,'pool_leaves',side_effect=lambda pool:leaves[pool]),patch.object(z,'run',return_value=rows) as run:
            z.cleanup_orphan_restore_pools('/dev/one')
        self.assertEqual([c.args for c in run.call_args_list if c.args[:2]==('zpool','export')],[('zpool','export',one)])
        self.assertFalse(any(c.args[0]=='zfs' for c in run.call_args_list))


class CompressionOptionTests(unittest.TestCase):
    def test_restore_streams_carry_source_properties_and_allow_recompression(self):
        for m in (native_fixture(),ubuntu_fixture()):
            for pool in m['pools']:
                with self.subTest(pool=pool['name']),patch.object(z,'native_dataset_names',return_value=z.native_expected_names(pool)):
                    for sender in (z.native_send(pool,m['snapshot']),z.incremental_send_argv(pool,m['snapshot'],'base')):
                        self.assertIn('-b',sender)
                        self.assertNotIn('-c',sender);self.assertNotIn('-w',sender)

    def test_cli_accepts_compression_for_backup_and_restore(self):
        for command in ('backup','restore'):
            with self.subTest(command=command),patch.object(z.os,'geteuid',return_value=0),patch.object(z,command) as handler:
                self.assertEqual(z.main([command,'--compression','zstd-3'],restore_isolated=True),0)
                self.assertEqual(handler.call_args.args[0].compression,'zstd-3')

    def test_invalid_methods_fail_before_operations(self):
        for method in ('gzip-0','gzip-10','zstd-0','zstd-20','zstd-fast-11','zstd-fast-1001','lz4;false'):
            with self.subTest(method=method),patch.object(z,'backup') as backup,patch('sys.stderr',new_callable=io.StringIO):
                with self.assertRaises(SystemExit) as error:z.main(['backup','--compression',method])
                self.assertEqual(error.exception.code,2)
                backup.assert_not_called()

    def test_supported_compression_levels(self):
        for method in ('off','on','lz4','lzjb','zle','gzip','gzip-1','gzip-9','zstd','zstd-1','zstd-19',
                       'zstd-fast','zstd-fast-1','zstd-fast-10','zstd-fast-20','zstd-fast-100','zstd-fast-500','zstd-fast-1000'):
            with self.subTest(method=method):self.assertEqual(z.compression_method(method),method)

    def test_override_on_filesystem_and_volume_receivers(self):
        for volume in (False,True):
            command=z.native_receive('store/target',volume=volume,compression='off')
            self.assertEqual(command[-3:],['-o','compression=off','store/target'])
            self.assertIn('readonly=on',command)
            self.assertFalse(any(a.startswith('compression=') for a in z.native_receive('store/target',volume=volume)))

    def test_dedicated_boot_pool_is_excluded_without_testing_or_enabling_features(self):
        m=ubuntu_fixture();original=copy.deepcopy(m['pools'][0])
        m['pools'][1]['properties']['feature@zstd_compress']={'value':'enabled'}
        with patch.object(z,'run') as run:z.restore_compression(m,'zstd-3')
        run.assert_not_called()
        self.assertEqual(m['pools'][0],original)
        self.assertEqual(m['pools'][1]['_restore_compression'],'zstd-3')

    def test_boot_pool_detected_by_mountpoint_with_another_name(self):
        m=fixture();m['pools'].append({'name':'bootstuff'})
        m['mounts'].append(dict(source='bootstuff/BOOT/os',target='/boot',fstype='zfs'))
        self.assertEqual(z.boot_pool_names(m),{'bootstuff'})

    def test_proxmox_root_pool_gets_selected_compression(self):
        m=fixture();m['boot']['manager']='proxmox-grub'
        m['pools'][0]['properties']['feature@zstd_compress']={'value':'active'}
        m['mounts'].append(dict(source=m['root_dataset'],target='/boot',fstype='zfs'))
        self.assertEqual(z.boot_pool_names(m),set())
        z.restore_compression(m,'zstd-9')
        self.assertEqual(m['pools'][0]['_restore_compression'],'zstd-9')

    def test_old_internal_override_is_not_accepted_from_manifest(self):
        m=ubuntu_fixture()
        for pool in m['pools']:pool['_restore_compression']='zstd-19'
        z.restore_compression(m,None)
        self.assertTrue(all('_restore_compression' not in p for p in m['pools']))

    def test_restore_rejects_missing_feature_before_destination_actions(self):
        m=native_fixture()
        args=z.argparse.Namespace(compression='zstd',snapshot=None,incremental=False)
        with patch.object(z,'select_backup',return_value=Path('/repo')), \
             patch.object(z,'verify_chain',return_value=[(Path('/repo'),m)]), \
             patch.object(z,'commands'),patch.object(z,'choose_destination') as choose,patch.object(z,'clone_restore') as restore:
            with self.assertRaisesRegex(z.Error,'saved pool features'):z.restore_from_storage(args,Path('/repo'))
        choose.assert_not_called();restore.assert_not_called()

    def test_destination_features_are_checked_without_enabling_them(self):
        for value in ('active','enabled','disabled','-'):
            with self.subTest(value=value),patch.object(z,'run',return_value=value) as run:
                if value in ('active','enabled'):z.check_compression_pool('zstd-3','target')
                else:
                    with self.assertRaisesRegex(z.Error,'does not enable'):z.check_compression_pool('zstd-3','target')
                run.assert_called_once_with('zpool','get','-H','-o','value','feature@zstd_compress','target')

    def test_raw_encrypted_backup_rejected_before_opening_destination(self):
        for m in (fixture(),ubuntu_fixture()):
            m['pools'][0]['encrypted']=True
            with self.subTest(pool=m['pools'][0]['name']),patch.object(z,'commands'), \
                 patch.object(z,'discover',return_value=m),patch.object(z,'backup_storage') as storage:
                with self.assertRaisesRegex(z.Error,'raw encrypted'):z.backup(z.argparse.Namespace(compression='gzip-6',incremental=False))
            storage.assert_not_called()

    def test_raw_encrypted_restore_rejected_except_exempt_boot_pool(self):
        m=ubuntu_fixture();m['pools'][1]['encrypted']=True
        with self.assertRaisesRegex(z.Error,'raw encrypted'):z.restore_compression(m,'gzip-6')
        m['pools'][1]['encrypted']=False;m['pools'][0]['encrypted']=True
        z.restore_compression(m,'gzip-6')
        self.assertNotIn('_restore_compression',m['pools'][0])
        self.assertEqual(m['pools'][1]['_restore_compression'],'gzip-6')


class PrivateStorageNamespaceTests(unittest.TestCase):
    def test_private_namespace_is_created_once_before_temporary_mounts(self):
        with patch.object(z,'PRIVATE_STORAGE_NAMESPACE',False),patch.object(z.ctypes,'CDLL') as library,patch.object(z,'run') as run:
            library.return_value.unshare.return_value=0
            z.private_storage_namespace();z.private_storage_namespace()
            library.return_value.unshare.assert_called_once_with(0x00020000)
            run.assert_called_once_with('mount','--make-rprivate','/')
            self.assertTrue(z.PRIVATE_STORAGE_NAMESPACE)

    def test_unshare_failure_does_not_change_mount_propagation(self):
        with patch.object(z,'PRIVATE_STORAGE_NAMESPACE',False),patch.object(z.ctypes,'CDLL') as library, \
             patch.object(z.ctypes,'get_errno',return_value=1),patch.object(z,'run') as run:
            library.return_value.unshare.return_value=-1
            with self.assertRaises(OSError):z.private_storage_namespace()
            run.assert_not_called();self.assertFalse(z.PRIVATE_STORAGE_NAMESPACE)


class DestinationCompressionDisplayTests(unittest.TestCase):
    def test_uses_destination_zfs_ratios_without_computing_from_source_sizes(self):
        boot=f'restore_boot\t{z.GIB}\t0\t{z.GIB}\t1.00\n'
        root=(f'restore_root\t{2*z.GIB}\t{z.GIB}\t{100*z.GIB}\t2.73\n'
              f'restore_root/ROOT\t{5*z.GIB}\t{2*z.GIB}\t{20*z.GIB}\t2.00\n'
              f'restore_root/vm\t{10*z.GIB}\t{5*z.GIB}\t{50*z.GIB}\t3.33\n')
        with patch.object(z,'run',side_effect=[boot,root]) as run,patch('sys.stdout',new_callable=io.StringIO) as output:
            z.report_destination_compression('Restore',[('bpool','restore_boot'),('rpool','restore_root')])
        self.assertEqual([c.args[-1] for c in run.call_args_list],['restore_boot','restore_root'])
        self.assertTrue(all(c.args[-2]=='name,usedbydataset,usedbysnapshots,logicalused,compressratio' for c in run.call_args_list))
        self.assertIn('including retained snapshots',output.getvalue())
        self.assertIn('bpool: compressed 1.000 GiB, uncompressed 1.000 GiB | ratio 1.00x',output.getvalue())
        self.assertIn('rpool: compressed 25.000 GiB, uncompressed 100.000 GiB | ratio 2.73x',output.getvalue())
        self.assertIn('Overall: compressed 26.000 GiB, uncompressed 101.000 GiB | ratio 3.88x',output.getvalue())

    def test_missing_statistic_does_not_abort_or_hide_other_pools(self):
        good=f'store/root\t{z.GIB}\t0\t{2*z.GIB}\t1.48\n'
        for problem in (z.Error('cannot get property'),OSError('read failed'),'-','not a ratio'):
            with self.subTest(problem=problem),patch.object(z,'run',side_effect=[problem,good]) as run, \
                 patch('sys.stdout',new_callable=io.StringIO) as output:
                z.report_destination_compression('Backup',[('bpool','store/boot'),('rpool','store/root')])
                self.assertIn('bpool: compressed unavailable, uncompressed unavailable | ratio unavailable',output.getvalue())
                self.assertIn('rpool: compressed 1.000 GiB, uncompressed 2.000 GiB | ratio 1.48x',output.getvalue())
                self.assertIn('Overall: compressed unavailable, uncompressed unavailable | ratio unavailable',output.getvalue())
                self.assertEqual(run.call_count,2)

    def test_incomplete_sizes_do_not_hide_valid_ratio_or_invent_totals(self):
        data=f'store/root\t{z.GIB}\t0\t{3*z.GIB}\t2.35x\nstore/root/child\t-\t0\t-\t-\n'
        with patch.object(z,'run',return_value=data),patch('sys.stdout',new_callable=io.StringIO) as output:
            z.report_destination_compression('Backup',[('rpool','store/root')])
        self.assertIn('rpool: compressed unavailable, uncompressed 3.000 GiB | ratio 2.35x',output.getvalue())
        self.assertIn('Overall: compressed unavailable, uncompressed 3.000 GiB | ratio unavailable',output.getvalue())

    def test_empty_dataset_reports_zero_sizes(self):
        with patch.object(z,'run',return_value='store/empty\t0\t0\t0\t1.00\n'), \
             patch('sys.stdout',new_callable=io.StringIO) as output:
            z.report_destination_compression('Backup',[('rpool','store/empty')])
        self.assertIn('rpool: compressed 0.000 GiB, uncompressed 0.000 GiB | ratio 1.00x',output.getvalue())
        self.assertIn('Overall: compressed 0.000 GiB, uncompressed 0.000 GiB | ratio 1.00x',output.getvalue())

    def test_overall_ratio_uses_byte_totals_before_display_rounding(self):
        rows=['boot\t1\t0\t1\t1.00\n','root\t100\t0\t200\t2.00\n']
        with patch.object(z,'run',side_effect=rows),patch('sys.stdout',new_callable=io.StringIO) as output:
            z.report_destination_compression('Backup',[('bpool','boot'),('rpool','root')])
        self.assertIn('Overall: compressed 0.000 GiB, uncompressed 0.000 GiB | ratio 1.99x',output.getvalue())

    def test_boot_exemption_identifies_selection_reason_and_saved_methods(self):
        m=ubuntu_fixture();boot=m['pools'][0]
        boot['datasets']['bpool']['compression']={'value':'lz4'}
        boot['datasets']['bpool/BOOT/ubuntu_1opcom']['compression']={'value':'gzip-6'}
        boot['datasets']['bpool@'+m['snapshot']]['compression']={'value':'zstd-19'}
        original=copy.deepcopy(boot)
        with patch('sys.stdout',new_callable=io.StringIO) as output:
            z.restore_compression(m,'off')
        self.assertIn('bpool: --compression off skipped for boot compatibility',output.getvalue())
        self.assertIn('keeping saved per-dataset compression (gzip-6, lz4)',output.getvalue())
        self.assertNotIn('zstd-19',output.getvalue())
        self.assertEqual(boot,original)
        self.assertEqual(m['pools'][1]['_restore_compression'],'off')

    def test_missing_saved_settings_does_not_invent_a_boot_compression_method(self):
        with patch('sys.stdout',new_callable=io.StringIO) as output:
            z.restore_compression(ubuntu_fixture(),'gzip-6')
        self.assertIn('--compression gzip-6 skipped for boot compatibility; keeping saved per-dataset compression.',output.getvalue())
        self.assertNotIn('(lz4)',output.getvalue())

    def test_no_exemption_notice_without_a_selection_or_separate_boot_pool(self):
        for manifest,method in ((ubuntu_fixture(),None),(native_fixture(),'gzip-6')):
            with patch('sys.stdout',new_callable=io.StringIO) as output:
                z.restore_compression(manifest,method)
            self.assertNotIn('skipped',output.getvalue())


class BackupStorePartitionTests(unittest.TestCase):
    def test_whole_disk_signature_does_not_replace_or_hide_missing_and_extra_partitions(self):
        for count in (0,2):
            node=dict(path='/dev/dest',type='disk',fstype='zfs_member',label='rpool',
                      children=[dict(path=f'/dev/dest{i}',type='part',fstype='zfs_member') for i in range(1,count+1)])
            with self.subTest(count=count),patch.object(z,'stable_device',side_effect=str), \
                 patch.object(z,'node_for',return_value=node),patch.object(z,'run') as run, \
                 patch.object(z,'backup_pool_usage') as usage:
                with self.assertRaisesRegex(z.Error,f'found {count}'):
                    with z.existing_store_locked('/dev/dest',None):pass
                run.assert_not_called();usage.assert_not_called()


class RestoreReleaseFixTests(unittest.TestCase):
    def test_saved_sync_values_and_inheritance_are_restored_before_flush(self):
        pool={'name':'rpool','datasets':{
            'rpool':{'sync':{'value':'standard','source':'local'}},
            'rpool/db':{'sync':{'value':'always','source':'local'}},
            'rpool/inherited':{'sync':{'value':'standard','source':'inherited from rpool'}},
            'rpool/async':{'sync':{'value':'disabled','source':'local'}},
            'rpool/db@snap':{'sync':{'value':'always'}}}}
        with patch.object(z,'run') as run:z.finish_restore_writes(pool,'target')
        calls=[c.args for c in run.call_args_list]
        self.assertEqual(calls[0],('zfs','set','sync=standard','target'))
        self.assertIn(('zfs','set','sync=always','target/db'),calls)
        self.assertIn(('zfs','set','sync=disabled','target/async'),calls)
        self.assertIn(('zfs','inherit','sync','target/inherited'),calls)
        self.assertEqual(calls[-1],('zpool','sync','target'))
        self.assertFalse(any('@' in str(c) for c in calls))

    def test_flush_failure_is_not_success(self):
        with patch.object(z,'run',side_effect=z.Error('sync failed')):
            with self.assertRaisesRegex(z.Error,'sync failed'):
                z.finish_restore_writes({'name':'rpool','datasets':{}},'target')

    def test_full_restore_sends_only_selected_snapshots_parent_first(self):
        pool=native_fixture()['pools'][0]
        with patch.object(z,'native_dataset_names',return_value=z.native_expected_names(pool)):
            streams=z.full_restore_streams(pool,'selected',stack=False)
            stacked=z.full_restore_streams(pool,'selected',stack=True)
        self.assertEqual([r for r,_ in streams],['','/ROOT','/ROOT/ubuntu'])
        for relative,argv in streams:
            self.assertEqual(argv,['zfs','send','-p','-b',pool['native_dataset']+relative+'@selected'])
        self.assertEqual(len(stacked),1);self.assertIn('-R',stacked[0][1])

    def test_selected_snapshot_estimates_sum_all_dataset_streams(self):
        pool=native_fixture()['pools'][0]
        with patch.object(z,'native_dataset_names',return_value=z.native_expected_names(pool)), \
             patch.object(z,'run',side_effect=['size\t10','size\t20','size\t30']):
            self.assertEqual(z.estimate_native_send(pool,'selected',stack=False),60)

    def test_selected_volume_receive_excludes_filesystem_properties(self):
        pool=native_fixture()['pools'][0]
        pool['datasets']['tank/volume']={'type':{'value':'volume'},'encryption':{'value':'off'}}
        receiver=['zfs','receive','-u','-F','-x','sync','-o','canmount=off','-o','sharenfs=off',
                  '-o','sharesmb=off','-o','volmode=none','target']
        def transfer(sender,receiver,*args,**kwargs):
            kwargs['on_bytes'](100)
            return 100
        with patch.object(z,'native_dataset_names',return_value=z.native_expected_names(pool)), \
             patch.object(z,'pipe_transfer',side_effect=transfer) as pipe:
            self.assertEqual(z.receive_full_restore(pool,'selected',receiver,False),400)
        volume=next(c.args[1] for c in pipe.call_args_list if c.args[1][-1]=='target/volume')
        self.assertNotIn('canmount=off',volume);self.assertNotIn('sharenfs=off',volume)
        self.assertNotIn('sharesmb=off',volume);self.assertIn('volmode=none',volume)

    def test_incremental_prunes_older_snapshots_but_stack_preserves_them(self):
        m,source,target,base,entry=incremental_restore_fixture();pool=m['pools'][0]
        for relative in ('','/ROOT','/ROOT/ubuntu'):
            source[relative+'@older']=entry('snapshot',900+len(relative),5)
            target[relative+'@older']=copy.deepcopy(source[relative+'@older'])
        plain=z.incremental_pool_plan(pool,m['snapshot'],source,target)
        stacked=z.incremental_pool_plan(pool,m['snapshot'],source,target,stack=True)
        self.assertIn('@older',plain['remove_snapshots'])
        self.assertNotIn('@older',stacked['remove_snapshots'])
        target['@older']['holds']='1'
        with self.assertRaisesRegex(z.Error,'holds or dependent clones'):
            z.incremental_pool_plan(pool,m['snapshot'],source,target)
        z.incremental_pool_plan(pool,m['snapshot'],source,target,stack=True)
        with patch.object(z,'native_dataset_names',return_value=z.native_expected_names(pool)):
            self.assertIn('-I',z.incremental_send_argv(pool,m['snapshot'],base,stack=True))
            self.assertIn('-i',z.incremental_send_argv(pool,m['snapshot'],base,stack=False))

    def test_incremental_swap_rejected_before_storage_access(self):
        with patch.object(z,'commands') as commands:
            with self.assertRaisesRegex(z.Error,'--swap applies only to full restore'):
                z.restore(z.argparse.Namespace(incremental=True,swap=8192))
        commands.assert_not_called()

    def test_unattended_encryption_rejected_before_target_access(self):
        m=native_fixture();m['pools'][0]['encrypted']=True
        args=z.argparse.Namespace(unattended=True)
        with patch.object(z,'select_backup',return_value=Path('/backup')), \
             patch.object(z,'verify_chain',return_value=[(Path('/backup'),m)]), \
             patch.object(z,'target_idle') as target,patch.object(z,'commands') as commands, \
             patch('builtins.input',side_effect=AssertionError('prompted')):
            with self.assertRaisesRegex(z.Error,'Unattended restore of encrypted pools'):
                z.restore_from_storage(args,Path('/backup'))
        target.assert_not_called();commands.assert_not_called()

    def test_fstab_remaps_exact_sources_without_touching_comments_or_other_disks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'etc').mkdir();path=root/'etc/fstab'
            path.write_text('# UUID=OLD stays in comment\nUUID=OLD /boot/efi vfat defaults 0 1\n'
                            'PARTUUID=OLD-P none swap sw 0 0\n/dev/disk/by-id/old-part1 /efi vfat defaults 0 1\n'
                            '/dev/disk/by-id/old-part10 /external vfat defaults 0 1\n')
            changes={'UUID=OLD':'UUID=NEW','PARTUUID=OLD-P':'PARTUUID=NEW-P',
                     '/dev/disk/by-id/old-part1':'/dev/disk/by-id/new-part1'}
            self.assertTrue(z.rewrite_restored_fstab(root,changes))
            text=path.read_text()
            self.assertIn('# UUID=OLD stays in comment',text)
            self.assertIn('UUID=NEW /boot/efi',text);self.assertIn('PARTUUID=NEW-P none',text)
            self.assertIn('/dev/disk/by-id/new-part1 /efi',text)
            self.assertIn('/dev/disk/by-id/old-part10 /external',text)
            self.assertFalse(z.rewrite_restored_fstab(root,changes))

    def test_fstab_identifiers_are_read_from_destination(self):
        layout=[dict(number=1,source_device='/dev/source1',partuuid='old-part',kind='esp',fat_uuid='AAAA-BBBB')]
        with patch.object(z,'partition_device',return_value='/dev/disk/by-id/target-part1'), \
             patch.object(z,'run',side_effect=['new-part','UUID=CCCC-DDDD\nTYPE=vfat\n']) as run:
            changes=z.restored_fstab_replacements('/dev/disk/by-id/target',layout)
        self.assertEqual(changes['/dev/source1'],'/dev/disk/by-id/target-part1')
        self.assertEqual(changes['UUID=AAAA-BBBB'],'UUID=CCCC-DDDD')
        self.assertEqual(changes['PARTUUID=old-part'],'PARTUUID=new-part')
        self.assertEqual(changes['/dev/disk/by-uuid/AAAA-BBBB'],'/dev/disk/by-uuid/CCCC-DDDD')
        self.assertTrue(all(c.args[-1]=='/dev/disk/by-id/target-part1' for c in run.call_args_list))

    def test_unattended_full_restore_keeps_original_swap_without_input(self):
        m=ubuntu_fixture();args=z.argparse.Namespace(target='/dev/target',unattended=True,confirm=True,dry_run=False)
        disk={'size':64*z.GIB,'log-sec':512,'phy-sec':512,'model':'test'}
        def execute(*args,**kwargs):
            if args[:2]==('zpool','reguid'):return '-g'
            if args==('zfs','version'):return 'zfs-kmod-2.3.0'
            return ''
        with patch.object(z,'stable_device',side_effect=str),patch.object(z,'select_backup',return_value=Path('/backup')), \
             patch.object(z,'verify_chain',return_value=[(Path('/backup'),m)]), \
             patch.object(z,'commands'),patch.object(z,'run',side_effect=execute), \
             patch.object(z,'estimate_native_send',side_effect=lambda p,*a,**kw:p['stream_bytes']), \
             patch.object(z,'protected_path',return_value=set()),patch.object(z,'target_idle',return_value=disk), \
             patch.object(z,'guid_conflicts',return_value=[]),patch.object(z,'print_existing_layout'), \
             patch.object(z,'discard_restore_target') as discard,patch.object(z,'clone_restore') as restore, \
             patch('builtins.input',side_effect=AssertionError('Unattended restore prompted')):
            z.restore_from_storage(args,Path('/backup'))
        planned=restore.call_args.args[3]['partitions']
        self.assertEqual(next(p['size_bytes'] for p in planned if p['kind']=='swap'),8*z.GIB)
        discard.assert_not_called()

    def test_fstab_changes_trigger_initramfs_rebuild_without_pool_identity_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'etc').mkdir();path=root/'etc/fstab'
            path.write_text('UUID=OLD /boot/efi vfat defaults 0 1\n')
            payload=dict(mounts=[dict(source='target/ROOT/os',target='/')],aliases=['target'],
                         fstab_replacements={'UUID=OLD':'UUID=NEW'})
            def rebuild(root):
                self.assertIn('UUID=NEW',path.read_text())
                return []
            with patch.object(z,'recovery_directory',return_value=z.contextlib.nullcontext(root)), \
                 patch.object(z,'run'),patch.object(z,'restored_executable',return_value=None), \
                 patch.object(z,'rebuild_restored_initramfs_tools',side_effect=rebuild) as initramfs, \
                 patch.object(z.os,'sync'):
                z.refresh_restored_dracut_worker(payload)
            initramfs.assert_called_once_with(root)
