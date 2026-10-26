"""Synthetic arrays used ONLY in unit tests. They are not weather observations."""
import datetime as dt
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import h5py
import numpy as np
from rasterio.crs import CRS
from rasterio.transform import from_bounds
from rasterio.warp import transform

spec=importlib.util.spec_from_file_location('worker',Path(__file__).resolve().parents[1]/'worker.py')
w=importlib.util.module_from_spec(spec);spec.loader.exec_module(w)
NOW=dt.datetime(2026,10,25,12,0,tzinfo=dt.timezone.utc)

def synthetic(path,when=NOW,quantity='DBZH',obj='COMP'):
    # Small georeferenced grid entirely inside Spain, north-up, 1 km cells.
    with h5py.File(path,'w') as f:
        what=f.create_group('what');what.attrs.update(object=obj,date=when.strftime('%Y%m%d'),time=when.strftime('%H%M%S'))
        where=f.create_group('where');where.attrs.update(projdef='EPSG:3857',LL_lon=-4.,LL_lat=40.,xscale=1000.,yscale=1000.,xsize=40,ysize=30)
        data=f.create_group('dataset1/data1');datawhat=data.create_group('what');datawhat.attrs.update(quantity=quantity,gain=.5,offset=-32.,nodata=255,undetect=0)
        raw=np.full((30,40),100,dtype=np.uint8);raw[0,:]=200;raw[10,10]=255;raw[11,11]=0
        data.create_dataset('data',data=raw)
    return path

class RadarWorkerTests(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def test_read_scaling_and_top_row(self):
        r=w.read_odim(synthetic(self.root/'fixture.h5'))
        self.assertEqual(r['dbz'][0,0],68);self.assertEqual(r['dbz'][5,5],18)
        self.assertLess(r['transform'].e,0);self.assertEqual(r['time'],NOW)
    def test_undetect_is_not_nodata(self):
        r=w.read_odim(synthetic(self.root/'fixture.h5'))
        self.assertTrue(np.isnan(r['dbz'][10,10]));self.assertEqual(r['observed'][10,10],0)
        self.assertEqual(r['dbz'][11,11],-999);self.assertEqual(r['observed'][11,11],1)
    def test_refuses_polar_volume(self):
        with self.assertRaises(ValueError):w.read_odim(synthetic(self.root/'fixture.h5',obj='PVOL'))
    def test_refuses_other_quantities(self):
        with self.assertRaises(ValueError):w.read_odim(synthetic(self.root/'fixture.h5',quantity='RATE'))
    def test_refuses_inconsistent_grid(self):
        p=synthetic(self.root/'fixture.h5')
        with h5py.File(p,'r+') as f:f['where'].attrs['xsize']=200
        with self.assertRaises(ValueError):w.read_odim(p)
    def test_refuses_missing_masks(self):
        p=synthetic(self.root/'fixture.h5')
        with h5py.File(p,'r+') as f:del f['dataset1/data1/what'].attrs['nodata']
        with self.assertRaises(ValueError):w.read_odim(p)
    def test_reprojection_and_nodata_image_are_distinct(self):
        xs,ys=transform('EPSG:4326','EPSG:3857',[w.BOUNDS[0],w.BOUNDS[2]],[w.BOUNDS[1],w.BOUNDS[3]])
        raw=np.full((80,90),25,dtype=np.float32);mask=np.ones_like(raw,dtype=np.uint8)
        raw[:20,:]=-999;raw[-20:,:]=np.nan;mask[-20:,:]=0
        radar={'dbz':raw,'observed':mask,'crs':CRS.from_epsg(3857),'transform':from_bounds(xs[0],ys[0],xs[1],ys[1],90,80)}
        rgba,coverage,stats=w.render_composite(radar,width=180)
        self.assertTrue(np.all(rgba[0:10,:,3]==0));self.assertTrue(np.all(coverage[0:10,:,3]==0))
        self.assertTrue(np.all(rgba[-10:,:,3]==0));self.assertTrue(np.all(coverage[-10:,:,3]>0))
        self.assertTrue(np.any(rgba[40:70,:,3]==255));self.assertGreater(stats['observedFraction'],.7)
    def test_future_and_old_files_rejected(self):
        for time in [NOW+dt.timedelta(hours=1),NOW-dt.timedelta(hours=7)]:
            with self.assertRaises(ValueError):w.make_frame(synthetic(self.root/'fixture.h5',when=time),self.root/'out','/radar-data',NOW)
    def test_frame_png_and_manifest_contract(self):
        frame=w.make_frame(synthetic(self.root/'fixture.h5'),self.root/'out','/radar-data',NOW)
        self.assertEqual(frame['id'],'20261025T120000Z')
        self.assertTrue((self.root/'out/frames/20261025T120000Z-r.png').read_bytes().startswith(b'\x89PNG'))
        self.assertTrue((self.root/'out/frames/20261025T120000Z-c.png').exists())
        m=w.make_manifest([frame],NOW);self.assertEqual(m['projection'],'EPSG:3857');self.assertEqual(m['unit'],'dBZ')
    def test_s3_discovery_midnight_and_quantity_filter(self):
        keys=['2026/10/25/OPERA/COMP/OPERA@20261025T0000@0@DBZH.h5','2026/10/24/OPERA/COMP/OPERA@20261024T2350@0@DBZH.h5','2026/10/25/OPERA/COMP/OPERA@20261025T0000@0@RATE.h5']
        xml=('<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'+''.join('<Contents><Key>'+k+'</Key></Contents>' for k in keys)+'<IsTruncated>false</IsTruncated></ListBucketResult>').encode()
        with patch.object(w,'request_bytes',return_value=xml) as fetch:
            found=w.discover(None,NOW.replace(hour=0,minute=5));self.assertEqual(len(found),2);self.assertEqual(fetch.call_count,2)
    def test_pagination_requires_continuation_cursor(self):
        with patch.object(w,'request_bytes',return_value=b'<ListBucketResult><IsTruncated>true</IsTruncated></ListBucketResult>'):
            with self.assertRaises(RuntimeError):w.discover(None,NOW)
    def test_selection_keeps_latest_and_bounded_history(self):
        items=[(NOW-dt.timedelta(minutes=i*5),str(i)) for i in reversed(range(40))]
        selected=w.select_frames(items);self.assertLessEqual(len(selected),13);self.assertEqual(selected[-1][0],NOW)
        self.assertGreaterEqual(selected[0][0],NOW-dt.timedelta(minutes=120))
    def test_public_urls_refuse_credentials_and_non_https(self):
        self.assertEqual(w.public_base('/radar-data'),'/radar-data')
        self.assertEqual(w.public_base('https://cdn.example/radar/'),'https://cdn.example/radar')
        for v in ['http://cdn.example','https://x:y@cdn.example','file:///etc/passwd','https://cdn.example?secret=1']:
            with self.assertRaises(ValueError):w.public_base(v)
    def test_upstream_download_cannot_be_an_arbitrary_proxy(self):
        with self.assertRaises(ValueError):w.request_bytes(None,'https://other.example/private')
    def test_failed_cycle_preserves_previous_manifest(self):
        output=self.root/'out';output.mkdir();previous=b'{"lastGood":true}';(output/'manifest.json').write_bytes(previous)
        args=SimpleNamespace(output=str(output),public_base='/radar-data',bucket='',prefix='radar',input=None,history=120,max_frames=13)
        with patch.object(w,'discover',return_value=[]):
            with self.assertRaises(RuntimeError):w.cycle(args,None)
        self.assertEqual((output/'manifest.json').read_bytes(),previous)
    def test_atomic_publication_and_cleanup(self):
        p=self.root/'manifest.json';w.atomic_json(p,{'ok':1});self.assertEqual(json.loads(p.read_text()),{'ok':1})
        out=self.root/'frames';out.mkdir();old=out/'20261023T120000Z-r.png';new=out/'20261025T120000Z-r.png';old.write_bytes(b'x');new.write_bytes(b'x')
        w.clean_local(self.root,NOW);self.assertFalse(old.exists());self.assertTrue(new.exists())
    def test_grid_cache_round_trip(self):
        g=np.full((4,5),np.nan,dtype=np.float32);g[1,1]=31.5;m=np.zeros((4,5),dtype=np.uint8);m[1,1]=1
        w.save_grid(self.root/'g.npz',g,m);dbz,mask=w.load_grid(self.root/'g.npz')
        self.assertEqual(dbz[1,1],31.5);self.assertTrue(np.isnan(dbz[0,0]));self.assertEqual(mask[1,1],1)
    def test_nowcast_moves_echo_downstream_without_creating_rain(self):
        try:import pysteps  # noqa: F401
        except ImportError:self.skipTest('pysteps not installed')
        grids=[]
        for step in range(3):
            g=np.full((120,160),np.nan,dtype=np.float32);y,x=np.ogrid[:120,:160]
            g[(y-60)**2+(x-(40+6*step))**2<=12**2]=40;grids.append(g)  # echo moving 6 px east per step
        out=w.extrapolate(grids,leads=3)
        self.assertEqual(out.shape,(3,120,160))
        cols=[np.nonzero(f>=w.STOPS[0])[1].mean() for f in out]
        self.assertGreater(cols[0],52);self.assertTrue(cols[0]<cols[1]<cols[2])
        self.assertLessEqual(float(out.max()),40.01)
    def test_nowcast_without_equally_spaced_inputs_publishes_observations_only(self):
        latest={'id':'20261025T120000Z','time':'2026-10-25T12:00:00Z'}
        frames=w.nowcast_frames(self.root,'/radar-data',latest,{NOW:'k'},lambda *a:None)
        self.assertEqual(frames,[])
    def test_nowcast_frames_contract(self):
        out=self.root/'out';lid='20261025T120000Z'
        g=np.full((30,40),np.nan,dtype=np.float32);g[10:15,10:15]=30;m=np.ones((30,40),dtype=np.uint8)
        w.save_grid(out/'frames'/f'{lid}.npz',g,m)
        with patch.object(w,'extrapolate',return_value=np.stack([g]*6)):
            frames=w.make_nowcast(out,'/radar-data',{'id':lid},[g,g,g])
        self.assertEqual([f['leadMinutes'] for f in frames],[10,20,30,40,50,60])
        self.assertEqual(frames[0]['id'],'20261025T121000Z');self.assertEqual(frames[-1]['time'],'2026-10-25T13:00:00Z')
        self.assertTrue(all(f['kind']=='forecast' and f['base']==lid for f in frames))
        self.assertEqual(frames[2]['image'],f'/radar-data/frames/{lid}-f30.png');self.assertEqual(frames[2]['coverage'],f'/radar-data/frames/{lid}-c.png')
        self.assertTrue((out/'frames'/f'{lid}-f60.png').read_bytes().startswith(b'\x89PNG'))
    def test_frames_report_echo_pixels_including_zero(self):
        frame=w.make_frame(synthetic(self.root/'fixture.h5'),self.root/'out','/radar-data',NOW)
        self.assertIsInstance(frame['echoPixels'],int);self.assertGreater(frame['echoPixels'],0)
        self.assertEqual(w.echo_pixels(np.zeros((4,4,4),dtype=np.uint8)),0)
    def test_cloud_layer_clear_sky_transparent_thick_cloud_visible(self):
        g=np.array([[30,55,72],[100,180,255]],dtype=np.uint8)
        c=w.cloud_layer(g)
        self.assertEqual(c[0,:,3].tolist(),[0,0,0])
        self.assertTrue(0<c[1,0,3]<c[1,1,3]);self.assertEqual(c[1,1,3],185);self.assertEqual(c[1,2,3],185)
    def test_rain_is_drawn_over_clouds_and_satellite_failure_keeps_rain(self):
        sat=w.Satellite(None,self.root,(4,2))
        rain=np.zeros((2,4,4),dtype=np.uint8);rain[0,0]=[30,120,220,255]
        with patch.object(sat,'gray',return_value=(np.full((2,4),200,dtype=np.uint8),NOW)):
            out=sat.composite(rain,NOW)
        self.assertEqual(out[0,0].tolist(),[30,120,220,255]);self.assertGreater(out[1,1,3],0)
        sat2=w.Satellite(None,self.root,(4,2))
        with patch.object(sat2,'gray',side_effect=RuntimeError('down')):
            self.assertIs(sat2.composite(rain,NOW),rain)
        self.assertTrue(sat2.failed)
    def test_satellite_requests_only_eumetview(self):
        sat=w.Satellite(None,self.root,(4,2))
        with self.assertRaises(ValueError):sat.request('https://evil.example/geoserver/ows',{})

if __name__=='__main__':unittest.main()
