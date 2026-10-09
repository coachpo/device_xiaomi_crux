"""Packaging contracts using synthetic inputs and mocked platform tools.

The CMS/comment fixture has valid envelope structure, not a trusted signature.
These tests do not generate keys or perform cryptographic signing; real trusted-key
verification remains the native Recovery integration check.
"""

import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import ssl
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
import warnings
import zipfile

SCRIPT = Path(__file__).resolve().parents[1] / "package_release.py"
SPEC = importlib.util.spec_from_file_location("package_release_under_test", SCRIPT)
PACKAGE_RELEASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PACKAGE_RELEASE)
SOURCE_KEY = "fixture/security/testkey"
NESTED_KEY = "fixture/security/media"
VERSION_INCREMENTAL = "2026100905"
VERSION_FP = "Xiaomi/crux/crux:13/TQ3A.230901.001.B1/2026100905:user/test-keys"
VERSION_DATE_UTC = "1791531809"
VERSION_DATE = "Fri Oct  9 15:43:29 CST 2026"


def generated_props(prefixes):
    fields = {"fingerprint": VERSION_FP, "version.incremental": VERSION_INCREMENTAL,
              "date.utc": VERSION_DATE_UTC, "date": VERSION_DATE}
    return ''.join(prefix + field + '=' + value + '\n'
                   for prefix in prefixes for field, value in fields.items()).encode()


def pem(contents):
    return "-----BEGIN CERTIFICATE-----\n" + base64.b64encode(contents).decode() + "\n-----END CERTIFICATE-----\n"


def der(tag, contents):
    length = len(contents)
    if length < 128:
        prefix = bytes([length])
    else:
        encoded = length.to_bytes((length.bit_length() + 7) // 8, "big")
        prefix = bytes([0x80 | len(encoded)]) + encoded
    return bytes([tag]) + prefix + contents


def synthetic_signature_comment():
    # Detached CMS SignedData shape with an intentionally invalid signature.
    version = der(0x02, b"\x01")
    digest = der(0x30, bytes.fromhex("0609608648016503040201") + b"\x05\x00")
    signature_algorithm = der(0x30, bytes.fromhex("06092a864886f70d010101") + b"\x05\x00")
    signer = der(0x30, version + der(0x30, der(0x30, b"") + version) + digest
                 + signature_algorithm + der(0x04, b"synthetic-not-a-valid-signature"))
    content = der(0x30, bytes.fromhex("06092a864886f70d010701"))
    signed_data = der(0x30, version + der(0x31, digest) + content + der(0x31, signer))
    signature = der(0x30, bytes.fromhex("06092a864886f70d010702") + der(0xa0, signed_data))
    message = b"signed by SignApk\x00"
    size = len(message) + len(signature) + 6
    return message + signature + (len(signature) + 6).to_bytes(2, "little") + b"\xff\xff" + size.to_bytes(2, "little")


SYNTHETIC_COMMENT = synthetic_signature_comment()
MOCK = r'''#!/usr/bin/env python3
import io,json,os,ssl,sys,zipfile
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
def values(flag):
    return [args[i+1] for i,a in enumerate(args[:-1]) if a == flag]
with open(os.environ['MOCK_CALLS'], 'a') as f: f.write(name+'\n')
if os.environ.get('MOCK_FAIL') == name: sys.exit(7)
inp,out=map(Path,args[-2:])
with zipfile.ZipFile(inp) as z: data={n:z.read(n) for n in z.namelist()}
if name == 'sign_target_files_apks':
    maps=dict(v.split('=',1) for v in values('--key_mapping'))
    certs=[Path(maps[k[:-9]]+'.x509.pem').read_bytes() for k in data['META/otakeys.txt'].decode().split()]
    for n in data:
        if n.endswith('/otacerts.zip'):
            buf=io.BytesIO()
            with zipfile.ZipFile(buf,'w') as z:
                for i,c in enumerate(certs): z.writestr(str(i)+'.x509.pem',c)
            data[n]=buf.getvalue()
        if n.endswith('mac_permissions.xml') and not os.environ.get('MOCK_STALE_SEINFO'):
            body=data[n].decode()
            for old,new in maps.items():
                old_der=ssl.PEM_cert_to_DER_cert(Path(old+'.x509.pem').read_text()).hex()
                new_der=ssl.PEM_cert_to_DER_cert(Path(new+'.x509.pem').read_text()).hex()
                body=body.replace(old_der,new_der)
            data[n]=body.encode()
    misc=dict(line.split('=',1) for line in data['META/misc_info.txt'].decode().splitlines())
    if not os.environ.get('MOCK_STALE_AVB'):
        for part in ('system','recovery'):
            misc['avb_'+part+'_key_path']=values('--avb_'+part+'_key')[0]
            misc['avb_'+part+'_algorithm']=values('--avb_'+part+'_algorithm')[0]
    data['META/misc_info.txt']='\n'.join(k+'='+v for k,v in misc.items()).encode()
elif name == 'ota_from_target_files':
    metadata='ota-type=BLOCK\npre-device=crux\npost-build=fixture/release-keys\n'
    if os.environ.get('MOCK_OTA_WIPE'): metadata+='ota-wipe=yes\n'
    refs=['system','vendor','boot','dtbo','vbmeta']
    if os.environ.get('MOCK_OTA_FIRMWARE'): refs.append('modem')
    if os.environ.get('MOCK_OTA_OMIT'): refs.remove(os.environ['MOCK_OTA_OMIT'])
    def block_path(partition):
        style=os.environ.get('MOCK_OTA_PATH_STYLE')
        direct=style=='direct' or (style=='mixed' and partition in ('system','vendor'))
        return ('/dev/block/by-name/' if direct else '/dev/block/bootdevice/by-name/')+partition
    script='\n'.join('assert(package_extract_file("'+p+'.img", "'+block_path(p)+'"));' for p in refs)
    if os.environ.get('MOCK_OTA_EXTRA_BLOCK'):
        script+='\nassert(package_extract_file("unexpected.img", "'+os.environ['MOCK_OTA_EXTRA_BLOCK']+'"));'
    data={'META-INF/com/android/metadata':metadata.encode(),
          'META-INF/com/google/android/updater-script':script.encode(),
          'META-INF/com/google/android/update-binary':b'mock-executable',
          'META-INF/com/android/otacert':Path(values('--package_key')[0]+'.x509.pem').read_bytes(),
          'boot.img':b'fixture','recovery.img':b'fixture','dtbo.img':b'fixture','vbmeta.img':b'fixture'}
    if os.environ.get('MOCK_OTACERT_MISMATCH'): data['META-INF/com/android/otacert']=b'wrong-public-certificate'
    if os.environ.get('MOCK_UNSIGNED'): data['META-INF/CERT.RSA']=b'JAR entry does not establish whole-file signing'
elif name == 'img_from_target_files':
    data={n[7:]:v for n,v in data.items() if n.startswith('IMAGES/') and n.endswith('.img')}
    data['android-info.txt']=b'require board=crux\n'
    if os.environ.get('MOCK_IMAGE_USERDATA'): data['userdata.img']=b'fixed-size-data'
with zipfile.ZipFile(out,'w') as z:
    for n,v in data.items(): z.writestr(n,v)
    if name=='ota_from_target_files' and not os.environ.get('MOCK_UNSIGNED'):
        comment=bytearray(Path(os.environ['MOCK_OTA_COMMENT']).read_bytes())
        malformed=os.environ.get('MOCK_BAD_FOOTER')
        if malformed=='sentinel': comment[-4:-2]=b'\x00\x00'
        elif malformed=='offset': comment[-6:-4]=(len(comment)+1).to_bytes(2,'little')
        elif malformed=='length': comment[-2:]=(len(comment)-1).to_bytes(2,'little')
        elif malformed=='second_eocd': comment[:4]=b'PK\x05\x06'
        z.comment=bytes(comment)
'''

MOCK_DEAPEXER = r'''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['MOCK_PROBES'],'a') as log: log.write(json.dumps(args)+'\n')
assert args[0]=='--debugfs_path' and Path(args[1]).is_file()
cmd=args[2]
if os.environ.get('MOCK_PROBE_FAIL'): sys.exit(5)
if cmd=='info':
    assert args[3]=='--print-type'
    print(json.loads(Path(args[4]).read_text())['type'])
elif cmd=='decompress':
    assert args[3]=='--input' and args[5]=='--output'
    source=json.loads(Path(args[4]).read_text())
    dest=Path(args[6]); assert not dest.exists()
    source['type']='UNCOMPRESSED'; dest.write_text(json.dumps(source))
elif cmd=='list':
    print('\n'.join(json.loads(Path(args[3]).read_text())['files']))
else: sys.exit(2)
'''


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='crux-package-fixture-')
        self.root = Path(self.tmp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.source_patch = mock.patch.object(PACKAGE_RELEASE, 'SOURCE_ROOT', self.source.resolve())
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)
        for prefix in (SOURCE_KEY, NESTED_KEY):
            certificate = self.source / (prefix + '.x509.pem')
            certificate.parent.mkdir(parents=True, exist_ok=True)
            certificate.write_text(pem(b'synthetic-old-certificate-' + prefix.encode()))
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()
        java = self.runtime / 'java'
        java.write_text('#!/bin/sh\nexit 42\n')
        java.chmod(0o755)
        self.keys = self.root / 'external-keys'
        self.keys.mkdir()
        self.newcert = pem(b'synthetic-new-certificate')
        (self.keys/'release.x509.pem').write_text(self.newcert)
        (self.keys/'release.pk8').write_text('MOCK PLACEHOLDER, NOT A PRIVATE KEY')
        for name in ('payload.pem','avb.pem'):
            (self.keys/name).write_text('MOCK PLACEHOLDER, NOT A PRIVATE KEY')
        self.host = self.root/'host'
        (self.host/'bin').mkdir(parents=True)
        for name in ('sign_target_files_apks','ota_from_target_files','img_from_target_files'):
            path = self.host/'bin'/name
            path.write_text("#!" + sys.executable + "\n" + MOCK.split("\n", 1)[1])
            path.chmod(0o755)
        for name,body in [('deapexer',MOCK_DEAPEXER),('debugfs_static','#!/bin/sh\nexit 0\n')]:
            path=self.host/'bin'/name
            path.write_text("#!" + sys.executable + "\n" + body.split("\n", 1)[1] if name == "deapexer" else body); path.chmod(0o755)
        self.calls = self.root/'calls.txt'
        self.probes = self.root/'probes.txt'
        comment=self.root/'synthetic-signature-comment.bin'
        comment.write_bytes(SYNTHETIC_COMMENT)
        self.env = {key: value for key, value in os.environ.items() if not key.startswith('MOCK_')}
        self.env.update(PATH=str(self.runtime)+os.pathsep+os.environ.get('PATH',''),
                        MOCK_CALLS=str(self.calls), MOCK_PROBES=str(self.probes),
                        MOCK_OTA_COMMENT=str(comment))
        self.target = self.root/'target_files.zip'
        self.configfile = self.root/'reviewed.json'
        old_der = ssl.PEM_cert_to_DER_cert((self.source/(SOURCE_KEY+'.x509.pem')).read_text()).hex()
        misc='default_system_dev_certificate='+SOURCE_KEY+'\nab_update=false\nuse_dynamic_partitions=false\navb_enable=true\navb_system_key_path=old.pem\navb_system_algorithm=SHA256_RSA2048\navb_recovery_key_path=old.pem\navb_recovery_algorithm=SHA256_RSA2048\navb_system_add_hashtree_footer_args=--prop com.android.build.system.fingerprint:'+VERSION_FP+'\navb_recovery_add_hash_footer_args=--prop=com.android.build.recovery.fingerprint:'+VERSION_FP+'\n'
        self.data = {
            'META/misc_info.txt':misc.encode(),
            'SYSTEM/build.prop':b'ro.build.flavor=aosp_crux_release-user\nro.product.system.device=crux\n'+generated_props(('ro.build.','ro.system.build.')),
            'VENDOR/build.prop':generated_props(('ro.vendor.build.',)),
            'RECOVERY/RAMDISK/prop.default':generated_props(('ro.build.','ro.system.build.','ro.vendor.build.','ro.odm.build.','ro.system_ext.build.')),
            'META/apkcerts.txt':('name="Example.apk" certificate="'+SOURCE_KEY+'.x509.pem" private_key="'+SOURCE_KEY+'.pk8"\n').encode(),
            'META/apexkeys.txt':('name="example.apex" public_key="old.avbpubkey" private_key="old.pem" container_certificate="'+SOURCE_KEY+'.x509.pem" container_private_key="'+SOURCE_KEY+'.pk8"\nname="prebuilt.apex" public_key="PRESIGNED" private_key="PRESIGNED" container_certificate="PRESIGNED" container_private_key="PRESIGNED"\n').encode(),
            'META/otakeys.txt':(SOURCE_KEY+'.x509.pem\n').encode(),
            'RECOVERY/RAMDISK/system/etc/recovery.fstab':(
                '# Ordinary stock routes; development fstab.crux_uboot is separate.\n' + '\n'.join(
                    '/dev/block/bootdevice/by-name/'+partition+' '+mount+' '+fstype+' defaults defaults'
                    for partition,mount,fstype in [('system','/system','ext4'),('vendor','/vendor','ext4'),
                        ('boot','/boot','emmc'),('recovery','/recovery','emmc'),('vbmeta','/vbmeta','emmc'),
                        ('misc','/misc','emmc'),('cache','/cache','ext4'),('userdata','/data','ext4'),
                        ('metadata','/metadata','ext4')]) + '\n').encode(),
            'BOOT/kernel':b'fixture kernel', 'RECOVERY/kernel':b'fixture kernel',
            'BOOT/RAMDISK/init':b'fixture init',
            'RECOVERY/RAMDISK/system/etc/security/otacerts.zip':self.certzip(b'old-certificate'),
            'SYSTEM/etc/selinux/plat_mac_permissions.xml':('<policy><signer signature="'+old_der+'"/></policy>').encode(),
            'VENDOR/bin/install-recovery.sh':b'#!/vendor/bin/sh\napplypatch --target EMMC:/dev/block/bootdevice/by-name/recovery:123:abc\n',
            'SYSTEM/app/Example/Example.apk':b'fixture flat APK',
            'SYSTEM/apex/example.apex':json.dumps({'type':'UNCOMPRESSED','files':['apex_manifest.pb']}).encode(),
            'SYSTEM/apex/prebuilt.apex':b'PRESIGNED fixture must not be opened by deapexer',
        }
        for p in ('boot','recovery','system','vendor','dtbo','vbmeta'):
            self.data['IMAGES/'+p+'.img']=b'fixture image '+p.encode()
        self.write_target()
        self.config = {'schema_version':1,'key_map':{SOURCE_KEY:'release'},'ota_package_key':'release',
            'apex':{'example.apex':{'container_key':'release','payload_key':'payload.pem'},
                    'prebuilt.apex':{'container_key':'PRESIGNED','payload_key':'PRESIGNED'}},
            'avb':{p:{'key':'avb.pem','algorithm':'SHA256_RSA2048'} for p in ('system','recovery')}}
        self.write_config()

    def tearDown(self):
        self.tmp.cleanup()

    def certzip(self, cert):
        buf=io.BytesIO()
        with zipfile.ZipFile(buf,'w') as z: z.writestr('old.x509.pem',cert)
        return buf.getvalue()

    def write_target(self, duplicate=None):
        with zipfile.ZipFile(self.target,'w') as z:
            for n,v in self.data.items(): z.writestr(n,v)
            if duplicate: z.writestr(duplicate,b'duplicate')

    def write_config(self):
        self.configfile.write_text(json.dumps(self.config))

    def invoke(self, *extra, expected=0, inspect=False):
        args=[str(SCRIPT)]
        if inspect:
            args += ['inspect','--target-files',str(self.target),'--host-tools',str(self.host)]
        else:
            args += ['package','--target-files',str(self.target),'--key-dir',str(self.keys),
                     '--config',str(self.configfile),'--host-tools',str(self.host),
                     '--output-dir',str(self.root/'output')]
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, 'argv', args+list(extra)), mock.patch.dict(os.environ, self.env, clear=True), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exit_code = PACKAGE_RELEASE.main()
        result = SimpleNamespace(returncode=exit_code, stdout=stdout.getvalue(), stderr=stderr.getvalue())
        self.assertEqual(result.returncode,expected,result.stdout+result.stderr)
        return result

    def rejected_before_commands(self, *extra):
        result=self.invoke(*extra,expected=1)
        self.assertFalse(self.calls.exists(),result.stdout+result.stderr)
        return result

    def test_inspect_uses_exact_apex_names_and_key_sources(self):
        result=json.loads(self.invoke(inspect=True).stdout)
        self.assertEqual(set(result['signing_configuration_template']['apex']),{'example.apex','prebuilt.apex'})
        self.assertEqual(result['signing_configuration_template']['key_map'],{SOURCE_KEY:None})

    def test_unused_global_test_declarations_do_not_require_release_keys(self):
        for prefix in ('cts/not-installed/testkey1','cts/not-installed/testkey2'):
            self.data['META/apkcerts.txt']+=('name="CtsUnused.apk" certificate="'+prefix+'.x509.pem" private_key="'+prefix+'.pk8" partition="data"\n').encode()
        self.data['META/apexkeys.txt']+=b'name="unused.test.apex" public_key="missing.avbpubkey" private_key="missing.pem" container_certificate="missing.x509.pem" container_private_key="missing.pk8"\n'
        self.write_target()
        result=json.loads(self.invoke(inspect=True).stdout)
        self.assertEqual(result['signing_configuration_template']['key_map'],{SOURCE_KEY:None})
        self.assertNotIn('unused.test.apex',result['signing_configuration_template']['apex'])
        self.invoke('--images')
        with zipfile.ZipFile(self.root/'output/signed-target_files.zip') as z:
            self.assertEqual(z.read('META/apkcerts.txt'),self.data['META/apkcerts.txt'])
            self.assertEqual(z.read('META/apexkeys.txt'),self.data['META/apexkeys.txt'])

    def test_identical_actual_apk_declarations_allow_partition_differences(self):
        self.data['META/apkcerts.txt']+=('name="Example.apk" certificate="'+SOURCE_KEY+'.x509.pem" private_key="'+SOURCE_KEY+'.pk8" partition="product"\n').encode()
        self.write_target()
        result=json.loads(self.invoke(inspect=True).stdout)
        self.assertEqual(result['inventory']['apk_keys'],{'Example.apk':SOURCE_KEY})
        self.invoke('--validate-only')

    def test_consumed_conflicting_signing_declaration_stops_before_signer(self):
        self.data['META/apkcerts.txt']+=b'name="Example.apk" certificate="fixture/security/media.x509.pem" private_key="fixture/security/media.pk8"\n'
        self.write_target()
        self.assertIn('Conflicting signing declarations for consumed package Example.apk',self.rejected_before_commands().stderr)

    def test_nested_apk_key_obligation_discovered_and_packaged(self):
        nested_key=NESTED_KEY
        self.data['SYSTEM/apex/example.apex']=json.dumps({'type':'UNCOMPRESSED','files':['./app/Nested/Nested.apk','lib64/libfixture.so']}).encode()
        self.data['META/apkcerts.txt']+=('name="Nested.apk" certificate="'+nested_key+'.x509.pem" private_key="'+nested_key+'.pk8"\n').encode()
        self.write_target()
        result=json.loads(self.invoke(inspect=True).stdout)
        self.assertEqual(result['inventory']['apk_keys']['Nested.apk'],nested_key)
        self.assertIn(nested_key,result['signing_configuration_template']['key_map'])
        (self.keys/'nested.x509.pem').write_text(self.newcert)
        (self.keys/'nested.pk8').write_text('MOCK PLACEHOLDER, NOT A PRIVATE KEY')
        self.config['key_map'][nested_key]='nested'; self.write_config()
        self.invoke('--images')
        report=json.loads((self.root/'output/package-report.json').read_text())
        self.assertEqual(report['shipping_inventory']['apk_keys']['Nested.apk'],nested_key)

    def test_unknown_nested_apk_stops_before_signer(self):
        self.data['SYSTEM/apex/example.apex']=json.dumps({'type':'UNCOMPRESSED','files':['./app/Unknown/Unknown.apk']}).encode()
        self.write_target()
        self.assertIn('Missing signing declaration for Unknown.apk',self.rejected_before_commands().stderr)

    def test_capex_normalization_uses_info_decompress_list(self):
        del self.data['SYSTEM/apex/example.apex']
        self.data['SYSTEM/apex/example.capex']=json.dumps({'type':'COMPRESSED','files':['apex_manifest.pb']}).encode()
        self.write_target()
        result=json.loads(self.invoke(inspect=True).stdout)
        self.assertEqual(result['inventory']['apex_paths']['SYSTEM/apex/example.capex'],'example.apex')
        calls=[json.loads(line)[2] for line in self.probes.read_text().splitlines()]
        self.assertEqual(calls,['info','decompress','list'])
        self.invoke('--images')

    def test_presigned_outer_apex_skips_nested_inspection_and_obligations(self):
        result=json.loads(self.invoke(inspect=True).stdout)
        self.assertIn('PRESIGNED',result['inventory']['apex_inspections']['SYSTEM/apex/prebuilt.apex']['reason'])
        calls=self.probes.read_text()
        self.assertNotIn('prebuilt',calls)

    def test_presigned_outer_concrete_key_override_stops_before_signer(self):
        self.config['apex']['prebuilt.apex']={'container_key':'release','payload_key':'payload.pem'}
        self.write_config()
        self.assertIn('Input PRESIGNED APEX must retain PRESIGNED container and payload: prebuilt.apex',self.rejected_before_commands().stderr)

    def test_metadata_declared_compressed_flat_apk_is_consumed(self):
        del self.data['SYSTEM/app/Example/Example.apk']
        self.data['SYSTEM/app/Example/Example.apk.gz']=b'mocked compressed APK'
        self.data['META/apkcerts.txt']=('name="Example.apk" certificate="'+SOURCE_KEY+'.x509.pem" private_key="'+SOURCE_KEY+'.pk8" compressed="gz"\n').encode()
        self.write_target()
        result=json.loads(self.invoke(inspect=True).stdout)
        self.assertEqual(result['inventory']['flat_apk_paths']['SYSTEM/app/Example/Example.apk.gz'],'Example.apk')
        self.invoke('--validate-only')

    def test_missing_apex_inspection_tool_or_probe_failure_stops_before_signer(self):
        (self.host/'bin/debugfs_static').unlink()
        self.assertIn('Missing executable platform releasetool',self.rejected_before_commands().stderr)
        (self.host/'bin/debugfs_static').write_text('#!/bin/sh\nexit 0\n')
        (self.host/'bin/debugfs_static').chmod(0o755)
        self.env['MOCK_PROBE_FAIL']='1'
        self.assertIn('APEX inspection failed',self.rejected_before_commands().stderr)

    def test_validate_only_creates_no_output_or_command(self):
        self.invoke('--images','--validate-only')
        self.assertFalse((self.root/'output').exists())
        self.assertFalse(self.calls.exists())

    def test_complete_package_calls_actual_interfaces_in_order_and_reports(self):
        self.invoke('--images')
        report=json.loads((self.root/'output/package-report.json').read_text())
        self.assertEqual(report['status'],'complete')
        self.assertEqual(self.calls.read_text().splitlines(),['sign_target_files_apks','ota_from_target_files','img_from_target_files'])
        signer=report['commands'][0]['argv']
        self.assertIn('--replace_ota_keys',signer)
        self.assertIn('example.apex='+str((self.keys/'release').resolve()),signer)
        self.assertIn('example.apex='+str((self.keys/'payload.pem').resolve()),signer)
        self.assertIn('prebuilt.apex=',signer)
        ota=report['commands'][1]['argv']
        self.assertIn('--package_key',ota)
        self.assertNotIn('--force_non_ab',ota)
        self.assertNotIn('--wipe_user_data',ota)
        self.assertEqual(ota[-2],report['commands'][2]['argv'][-2])
        self.assertEqual(len(report['ota']['updater_block_references']),5)
        self.assertEqual(report['ota']['whole_file_signature']['format'],'Android whole-file ZIP-comment signature')
        self.assertTrue(report['ota']['otacert_matches_selected_key'])
        with zipfile.ZipFile(self.root/'output/ota.zip') as z:
            self.assertFalse(any(n.endswith('.RSA') for n in z.namelist()))
        self.assertTrue((self.root/'output/install-recovery-VENDOR.sh').exists())
        self.assertFalse(any(p.suffix=='.pk8' for p in (self.root/'output').rglob('*')))

    def presigned_only(self):
        del self.data['SYSTEM/apex/example.apex']
        del self.config['apex']['example.apex']
        self.write_target()
        self.write_config()

    def assert_no_external_commands_or_output(self):
        self.assertFalse(self.calls.exists())
        self.assertFalse(self.probes.exists())
        self.assertFalse((self.root/'output').exists())

    def test_missing_key_fails_before_external_tool(self):
        self.presigned_only()
        (self.keys/'release.pk8').unlink()
        self.assertIn('Missing or empty key file',self.rejected_before_commands().stderr)
        self.assert_no_external_commands_or_output()

    def test_wrong_product_or_device_stops_before_any_external_command(self):
        for props in (b'ro.build.flavor=aosp_crux-userdebug\nro.product.system.device=crux\n',
                      b'ro.build.flavor=aosp_crux_release-user\nro.product.system.device=cepheus\n'):
            with self.subTest(props=props):
                self.data['SYSTEM/build.prop']=props
                self.write_target()
                self.rejected_before_commands()
                self.assert_no_external_commands_or_output()

    def test_ab_or_dynamic_product_stops_before_any_external_command(self):
        original=self.data['META/misc_info.txt']
        for old,new in [(b'ab_update=false',b'ab_update=true'),
                        (b'use_dynamic_partitions=false',b'use_dynamic_partitions=true')]:
            with self.subTest(property=new):
                self.data['META/misc_info.txt']=original.replace(old,new)
                self.write_target()
                self.assertIn('Only static non-A/B target-files',self.rejected_before_commands().stderr)
                self.assert_no_external_commands_or_output()

    def test_unfilled_signing_configuration_fails_before_external_tool(self):
        self.config['apex']['example.apex']['payload_key']=None
        self.write_config()
        self.assertIn('Signing configuration needs a key path',self.rejected_before_commands().stderr)

    def test_apex_mismatch_rejected_without_signing(self):
        self.config['apex'].pop('example.apex')
        self.write_config()
        self.assertIn('apex map must cover exactly',self.rejected_before_commands().stderr)

    def test_absent_avb_metadata_slot_rejected_before_tool(self):
        self.config['avb']['vbmeta']={'key':'avb.pem','algorithm':'SHA256_RSA2048'}
        self.write_config()
        self.assertIn('cannot persist absent AVB metadata',self.rejected_before_commands().stderr)

    def test_firmware_userdata_and_bootable_bypass_are_rejected(self):
        for name in ('RADIO/modem.img','INSTALL/firmware-update/abl.elf','IMAGES/userdata.img','IMAGES/modem.img','BOOTABLE_IMAGES/boot.img'):
            with self.subTest(name=name):
                self.data[name]=b'fixture'
                self.write_target()
                self.rejected_before_commands()
                del self.data[name]

    def test_duplicate_image_rejected_before_tool(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            self.write_target(duplicate='IMAGES/boot.img')
        self.rejected_before_commands()

    def test_prebuilt_boot_and_recovery_rejected_before_signer(self):
        for name in ('PREBUILT_IMAGES/boot.img','PREBUILT_IMAGES/recovery.img'):
            with self.subTest(name=name):
                self.data[name]=b'prebuilt fixture with old routing or OTA certificate'
                self.write_target()
                result=self.rejected_before_commands()
                self.assertIn('Prebuilt Boot/Recovery bypasses rebuilt routing and OTA certificates',result.stderr)
                self.assertIn(name,result.stderr)
                del self.data[name]

    def test_missing_tool_clear_stop(self):
        (self.host/'bin/sign_target_files_apks').unlink()
        self.assertIn('Missing executable platform releasetool',self.rejected_before_commands().stderr)

    def test_missing_java_on_path_stops_before_output_or_signer(self):
        self.presigned_only()
        runtime=self.root/'empty-path'; runtime.mkdir()
        self.env['PATH']=str(runtime)
        for flags in [[],['--validate-only']]:
            with self.subTest(flags=flags):
                result=self.rejected_before_commands(*flags)
                self.assertIn('Platform signing requires executable java on PATH',result.stderr)
                self.assert_no_external_commands_or_output()

    def test_java_preflight_does_not_launch_java(self):
        runtime=self.root/'minimal-path'; runtime.mkdir()
        java=runtime/'java'
        marker=self.root/'java-was-launched'
        java.write_text('#!/bin/sh\n: > '+str(marker)+'\nexit 42\n')
        java.chmod(0o755)
        self.env['PATH']=str(runtime)
        self.invoke('--validate-only')
        self.assertFalse(marker.exists())
        self.assertFalse(self.calls.exists())
        self.assertFalse((self.root/'output').exists())

    def test_keys_inside_output_rejected(self):
        output=self.root/'output'
        self.keys.rename(output)
        self.keys=output
        self.assertIn('Keep private keys outside',self.rejected_before_commands().stderr)

    def test_tool_failure_preserved_and_later_steps_not_run(self):
        self.env['MOCK_FAIL']='sign_target_files_apks'
        self.invoke(expected=1)
        report=json.loads((self.root/'output/package-report.json').read_text())
        self.assertEqual(report['status'],'failed')
        self.assertEqual(report['commands'][0]['exit_code'],7)
        self.assertEqual(self.calls.read_text().splitlines(),['sign_target_files_apks'])

    def test_stale_ota_certificates_stop_before_ota(self):
        self.data['RECOVERY/RAMDISK/system/etc/security/otacerts.zip']=self.certzip(b'old-certificate')
        self.data['META/otakeys.txt']=b''
        self.write_target()
        self.invoke(expected=1)
        report=json.loads((self.root/'output/package-report.json').read_text())
        self.assertEqual(report['status'],'failed')
        self.assertEqual(self.calls.read_text().splitlines(),['sign_target_files_apks'])

    def test_stale_seinfo_and_avb_each_stop_before_ota(self):
        for envkey in ('MOCK_STALE_SEINFO','MOCK_STALE_AVB'):
            with self.subTest(envkey=envkey):
                self.env[envkey]='1'
                self.invoke(expected=1)
                self.assertEqual(self.calls.read_text().splitlines(),['sign_target_files_apks'])
                shutil.rmtree(self.root/'output')
                self.calls.unlink()
                del self.env[envkey]

    def test_ota_wipe_and_firmware_rejected_and_images_not_called(self):
        for envkey in ('MOCK_OTA_WIPE','MOCK_OTA_FIRMWARE'):
            with self.subTest(envkey=envkey):
                self.env[envkey]='1'
                self.invoke('--images',expected=1)
                report=json.loads((self.root/'output/package-report.json').read_text())
                self.assertEqual(report['status'],'failed')
                self.assertEqual(self.calls.read_text().splitlines(),['sign_target_files_apks','ota_from_target_files'])
                shutil.rmtree(self.root/'output')
                self.calls.unlink()
                del self.env[envkey]

    def test_final_image_bundle_rejects_userdata(self):
        self.env['MOCK_IMAGE_USERDATA']='1'
        self.invoke('--images',expected=1)
        report=json.loads((self.root/'output/package-report.json').read_text())
        self.assertEqual(report['status'],'failed')
        self.assertIn('unexpected partition/firmware/userdata',report['error'])

    def test_jar_certificate_without_whole_file_footer_is_rejected(self):
        self.env['MOCK_UNSIGNED']='1'
        self.invoke('--images',expected=1)
        report=json.loads((self.root/'output/package-report.json').read_text())
        self.assertIn('whole-file signature footer',report['error'])
        self.assertEqual(self.calls.read_text().splitlines(),['sign_target_files_apks','ota_from_target_files'])

    def test_malformed_whole_file_footer_is_rejected(self):
        for malformed in ('sentinel','offset','length','second_eocd'):
            with self.subTest(malformed=malformed):
                self.env['MOCK_BAD_FOOTER']=malformed
                self.invoke('--images',expected=1)
                report=json.loads((self.root/'output/package-report.json').read_text())
                self.assertEqual(report['status'],'failed')
                self.assertEqual(self.calls.read_text().splitlines(),['sign_target_files_apks','ota_from_target_files'])
                shutil.rmtree(self.root/'output'); self.calls.unlink()

    def test_mismatched_public_otacert_is_rejected(self):
        self.env['MOCK_OTACERT_MISMATCH']='1'
        self.invoke(expected=1)
        report=json.loads((self.root/'output/package-report.json').read_text())
        self.assertIn('otacert differs',report['error'])

    def test_direct_by_name_partitions_are_accepted(self):
        self.env['MOCK_OTA_PATH_STYLE']='direct'
        self.invoke('--images')
        report=json.loads((self.root/'output/package-report.json').read_text())
        self.assertEqual(report['status'],'complete')
        self.assertEqual(set(report['ota']['updater_partitions']),{'boot','system','vendor','dtbo','vbmeta'})
        self.assertTrue(all(path.startswith('/dev/block/by-name/') for path in report['ota']['updater_block_references']))

    def test_mixed_public_by_name_partitions_are_accepted(self):
        self.env['MOCK_OTA_PATH_STYLE']='mixed'
        self.invoke('--images')
        report=json.loads((self.root/'output/package-report.json').read_text())
        self.assertEqual(report['status'],'complete')
        self.assertEqual(set(report['ota']['updater_partitions']),{'boot','system','vendor','dtbo','vbmeta'})
        paths=set(report['ota']['updater_block_references'])
        self.assertIn('/dev/block/by-name/system',paths)
        self.assertIn('/dev/block/by-name/vendor',paths)
        self.assertIn('/dev/block/bootdevice/by-name/boot',paths)

    def test_invalid_partition_or_noncanonical_path_is_rejected(self):
        paths=[prefix+partition for prefix in ('/dev/block/by-name/','/dev/block/bootdevice/by-name/')
               for partition in ('pesystem','pevendor','peuserdata','pemetadata','modem','misc','userdata','metadata','data','meta')]
        paths+=['/dev/block/platform/ufs/by-name/system','/dev/block/by-name/system/../vendor',
                '/dev/block/bootdevice/by-name/system/extra']
        for path in paths:
            with self.subTest(path=path):
                self.env['MOCK_OTA_EXTRA_BLOCK']=path
                self.invoke('--images',expected=1)
                report=json.loads((self.root/'output/package-report.json').read_text())
                self.assertIn('Unexpected updater block-device reference: '+path,report['error'])
                self.assertEqual(self.calls.read_text().splitlines(),['sign_target_files_apks','ota_from_target_files'])
                shutil.rmtree(self.root/'output'); self.calls.unlink()

    def test_missing_required_partition_identity_is_rejected(self):
        self.env['MOCK_OTA_PATH_STYLE']='mixed'
        self.env['MOCK_OTA_OMIT']='vendor'
        self.invoke('--images',expected=1)
        report=json.loads((self.root/'output/package-report.json').read_text())
        self.assertIn('OTA updater lacks an expected Android partition reference',report['error'])
        self.assertEqual(self.calls.read_text().splitlines(),['sign_target_files_apks','ota_from_target_files'])

    def test_coherent_generated_rom_version_is_reported(self):
        result=json.loads(self.invoke(inspect=True).stdout)
        self.assertEqual(result['inventory']['generated_version']['expected']['version.incremental'],VERSION_INCREMENTAL)
        self.assertEqual(result['inventory']['generated_version']['expected']['fingerprint'],VERSION_FP)
        self.assertIn('avb_system_add_hashtree_footer_args:com.android.build.system.fingerprint',
                      result['inventory']['generated_version']['avb_fingerprints'])

    def test_cached_global_incremental_with_new_fingerprint_is_rejected_early(self):
        self.data['SYSTEM/build.prop']=self.data['SYSTEM/build.prop'].replace(
            b'ro.build.fingerprint='+VERSION_FP.encode(),
            b'ro.build.fingerprint=Xiaomi/crux/crux:13/TQ3A.230901.001.B1/2026100906:user/test-keys')
        self.write_target()
        self.assertIn('ro.build.fingerprint build number differs from ro.build.version.incremental=2026100905',
                      self.rejected_before_commands().stderr)
        self.assert_no_external_commands_or_output()

    def test_stale_partition_or_recovery_generated_fields_are_rejected_early(self):
        changes=[('SYSTEM/build.prop','ro.system.build.fingerprint',VERSION_FP.replace('2026100905','2026100904')),
                 ('SYSTEM/build.prop','ro.system.build.version.incremental','2026100904'),
                 ('SYSTEM/build.prop','ro.system.build.date.utc','1791530000'),
                 ('VENDOR/build.prop','ro.vendor.build.fingerprint',VERSION_FP.replace('2026100905','2026100904')),
                 ('VENDOR/build.prop','ro.vendor.build.date.utc','1791530000'),
                 ('RECOVERY/RAMDISK/prop.default','ro.build.version.incremental','2026100904'),
                 ('RECOVERY/RAMDISK/prop.default','ro.vendor.build.fingerprint',VERSION_FP.replace('2026100905','2026100904')),
                 ('RECOVERY/RAMDISK/prop.default','ro.odm.build.date.utc','1791530000'),
                 ('RECOVERY/RAMDISK/prop.default','ro.system_ext.build.version.incremental','2026100904')]
        for path,key,value in changes:
            with self.subTest(path=path,key=key):
                original=self.data[path]
                rows=original.decode().splitlines()
                self.data[path]='\n'.join(key+'='+value if row.startswith(key+'=') else row for row in rows).encode()
                self.write_target()
                self.assertIn(path+': '+key+'=',self.rejected_before_commands().stderr)
                self.assert_no_external_commands_or_output()
                self.data[path]=original

    def test_stale_avb_fingerprint_is_rejected_early(self):
        original=self.data['META/misc_info.txt']
        for line in ['avb_vendor_boot_add_hash_footer_args=--prop com.android.build.vendor_boot.fingerprint:'+VERSION_FP.replace('2026100905','2026100904'),
                     'avb_system_ext_add_hashtree_footer_args=--prop=com.android.build.system_ext.fingerprint:'+VERSION_FP.replace('2026100905','2026100904')]:
            with self.subTest(line=line):
                self.data['META/misc_info.txt']=original+line.encode()+b'\n'
                self.write_target()
                self.assertIn('META/misc_info.txt:',self.rejected_before_commands().stderr)
                self.assert_no_external_commands_or_output()
        self.data['META/misc_info.txt']=original

    def test_missing_generated_version_field_is_rejected_early(self):
        self.data['VENDOR/build.prop']=self.data['VENDOR/build.prop'].replace(
            b'ro.vendor.build.version.incremental='+VERSION_INCREMENTAL.encode()+b'\n',b'')
        self.write_target()
        self.assertIn('Missing generated ROM version property: VENDOR/build.prop: ro.vendor.build.version.incremental',
                      self.rejected_before_commands().stderr)
        self.assert_no_external_commands_or_output()

    def test_unrelated_presigned_and_firmware_versions_are_not_rom_version_fields(self):
        self.data['SYSTEM/app/Example/Example.apk']=b'unrelated application version1901'
        self.data['SYSTEM/apex/prebuilt.apex']=b'preserved third-party version1901'
        self.data['VENDOR/firmware/thirdparty-version.txt']=b'old firmware build1901'
        self.data['VENDOR/build.prop']+=b'ro.vendor.build.security_patch=2021-10-01\n'
        self.data['SYSTEM/build.prop']+=b'ro.build.tags=release-keys\n'
        self.write_target()
        self.invoke('--validate-only')


if __name__ == '__main__':
    unittest.main(verbosity=2)
