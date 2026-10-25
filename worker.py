#!/usr/bin/env python3
"""OPERA DBZH -> Web Mercator PNG sequence. No screenshot extraction or nowcasting.

Source: EUMETNET Open Radar Data public 24h S3 cache (documented anonymous access).
Output contract: version 1, DBZH, dBZ, EPSG:3857, fixed Iberia/Balearic extent.
A failed cycle never replaces a valid published manifest with an empty sequence.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, urljoin, urlparse

import h5py
import numpy as np
from PIL import Image
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from rasterio.crs import CRS
from rasterio.transform import from_origin, from_bounds
from rasterio.warp import reproject, transform, Resampling

UTC=dt.timezone.utc
SOURCE='https://s3.waw3-1.cloudferro.com/openradar-24h/'
BOUNDS=(-11.5,34.5,5.5,45.6)
MAX_FILE=96*1024*1024
STOPS=np.array([5,15,25,35,45,55,65],dtype=np.float32)
COLORS=np.array([[115,198,223],[66,139,208],[72,177,131],[227,205,87],[236,145,68],[214,82,105],[159,105,186]],dtype=np.float32)
KEY_RE=re.compile(r'(?:^|/)OPERA@(\d{8}T\d{4}(?:\d{2})?)@[^/]+@DBZH\.h5$')
FRAME_RE=re.compile(r'^\d{8}T\d{6}Z$')
LOG=logging.getLogger('meteonexo.radar')

def utcnow(): return dt.datetime.now(UTC)
def iso(value): return value.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00','Z')
def attr(obj,key,default=None):
    value=obj.attrs.get(key,default) if obj is not None else default
    if isinstance(value,bytes):return value.decode('utf8')
    return value.item() if isinstance(value,np.generic) else value

def session():
    s=requests.Session()
    s.headers.update({'User-Agent':'MeteoNexo-Radar/1.0 (+EUMETNET-OPERA-consumer)'})
    retry=Retry(total=3,backoff_factor=1.5,status_forcelist=[429,500,502,503,504],allowed_methods=['GET'],respect_retry_after_header=True)
    s.mount('https://',HTTPAdapter(max_retries=retry))
    return s

def request_bytes(s,url,*,limit=MAX_FILE,params=None):
    # Every upstream URL is constructed here; no client-supplied fetch/proxy URLs.
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.netloc!='s3.waw3-1.cloudferro.com' or not parsed.path.startswith('/openradar-24h/'):
        raise ValueError('Unexpected source URL')
    with s.get(url,params=params,timeout=(15,90),stream=True,allow_redirects=False) as response:
        response.raise_for_status()
        if response.status_code!=200:raise RuntimeError('Source did not return a complete response')
        if int(response.headers.get('content-length','0'))>limit:raise ValueError('Source file exceeds limit')
        buf=bytearray()
        for chunk in response.iter_content(65536):
            buf.extend(chunk)
            if len(buf)>limit:raise ValueError('Source file exceeds limit')
        return bytes(buf)

def discover(s,now=None,history_minutes=120):
    now=now or utcnow();start=now-dt.timedelta(minutes=history_minutes+30)
    dates=sorted({start.date(),now.date()},reverse=True)
    out={}
    for day in dates:
        token=None
        for _ in range(8):
            params={'list-type':'2','prefix':day.strftime('%Y/%m/%d/')+'OPERA/COMP/','max-keys':'1000'}
            if token:params['continuation-token']=token
            root=ET.fromstring(request_bytes(s,SOURCE,params=params,limit=4*1024*1024))
            for item in root.findall('{*}Contents'):
                key=item.findtext('{*}Key','');match=KEY_RE.search(key)
                if not match:continue
                stamp=match.group(1)
                observed=dt.datetime.strptime(stamp,'%Y%m%dT%H%M' if len(stamp)==13 else '%Y%m%dT%H%M%S').replace(tzinfo=UTC)
                if start<=observed<=now+dt.timedelta(minutes=5):out[observed]=key
            token=root.findtext('{*}NextContinuationToken')
            if root.findtext('{*}IsTruncated','false')!='true':break
            if not token:raise RuntimeError('Truncated source listing without cursor')
        else:raise RuntimeError('Source pagination exceeded safety bound')
    return sorted(out.items())

def select_frames(items,max_frames=13,history_minutes=120):
    if not items:return []
    end=items[-1][0];cut=end-dt.timedelta(minutes=history_minutes)
    items=[x for x in items if x[0]>=cut]
    if len(items)<=max_frames:return items
    # Fixed 10-minute slots avoid redownloading the whole history on every cycle.
    step=math.ceil(history_minutes/max(1,max_frames-1)/5)*5
    chosen=[x for x in items if (x[0].hour*60+x[0].minute)%step==0]
    if items[-1] not in chosen:chosen.append(items[-1])
    return chosen[-max_frames:]

def read_odim(filename):
    with h5py.File(filename,'r') as f:
        rootwhat=f.get('what');rootwhere=f.get('where')
        if attr(rootwhat,'object')!='COMP':raise ValueError('Expected a cartesian OPERA composite, not a polar radar volume')
        found=None
        for dsname in sorted(k for k in f if k.startswith('dataset')):
            ds=f[dsname]
            for dname in sorted(k for k in ds if k.startswith('data')):
                group=ds[dname];what=group.get('what')
                if attr(what,'quantity')=='DBZH' and 'data' in group:
                    found=(ds,group,what);break
            if found:break
        if not found:raise ValueError('No DBZH composite dataset found')
        ds,group,what=found
        data=group['data']
        if len(data.shape)!=2 or math.prod(data.shape)>40_000_000 or data.dtype.kind not in 'uif':raise ValueError('Unexpected composite dimensions or numeric type')
        raw=data[()]
        where=ds.get('where') if ds.get('where') is not None else rootwhere
        if where is None:raise ValueError('Missing ODIM grid georeferencing')
        crs=CRS.from_string(attr(where,'projdef'))
        if not crs.is_projected or crs.linear_units not in ('metre','meter'):raise ValueError('Expected a projected metric grid')
        dx=float(attr(where,'xscale'));dy=float(attr(where,'yscale'))
        if not(100<=dx<=10000 and 100<=dy<=10000):raise ValueError('Unexpected radar grid scale')
        height,width=raw.shape
        if int(attr(where,'xsize',width))!=width or int(attr(where,'ysize',height))!=height:raise ValueError('Grid dimensions do not match data')
        # ODIM COMP matrices: row 0 is the north/top edge. LL identifies the
        # lower-left outer corner. Never place the image by radar-range circles.
        ll_lon=float(attr(where,'LL_lon'));ll_lat=float(attr(where,'LL_lat'))
        xs,ys=transform('EPSG:4326',crs,[ll_lon],[ll_lat])
        left,bottom=xs[0],ys[0];top=bottom+height*dy
        transform_src=from_origin(left,top,dx,dy)
        if attr(where,'UL_lat') is not None:
            xx,yy=transform('EPSG:4326',crs,[float(attr(where,'UL_lon'))],[float(attr(where,'UL_lat'))])
            if abs(xx[0]-left)>2*dx or abs(yy[0]-top)>2*dy:raise ValueError('Inconsistent ODIM grid corners')
        gain=float(attr(what,'gain'));offset=float(attr(what,'offset'))
        nodata=attr(what,'nodata');undetect=attr(what,'undetect')
        if nodata is None or undetect is None or nodata==undetect or not math.isfinite(gain) or gain<=0 or not math.isfinite(offset):raise ValueError('Missing or invalid radar scaling/masks')
        observed=(raw!=nodata)&np.isfinite(raw)
        dbz=raw.astype(np.float32)*gain+offset
        dbz[raw==undetect]=-999.0  # valid observation BELOW DETECTION; not missing data
        dbz[~observed]=np.nan
        stamp=str(attr(rootwhat,'date',''))+str(attr(rootwhat,'time',''))
        observation=dt.datetime.strptime(stamp,'%Y%m%d%H%M%S').replace(tzinfo=UTC)
        return {'dbz':dbz,'observed':observed.astype(np.uint8),'crs':crs,'transform':transform_src,
                'time':observation,'nativeScale':[dx,dy],'width':width,'height':height}

def render_composite(radar,width=1536):
    west,south,east,north=BOUNDS
    xx,yy=transform('EPSG:4326','EPSG:3857',[west,east],[south,north])
    height=round(width*(yy[1]-yy[0])/(xx[1]-xx[0]))
    dst_transform=from_bounds(xx[0],yy[0],xx[1],yy[1],width,height)
    target=np.full((height,width),np.nan,dtype=np.float32)
    mask=np.zeros((height,width),dtype=np.uint8)
    common=dict(src_transform=radar['transform'],src_crs=radar['crs'],dst_transform=dst_transform,dst_crs='EPSG:3857',resampling=Resampling.nearest,num_threads=2)
    reproject(radar['dbz'],target,src_nodata=np.nan,dst_nodata=np.nan,**common)
    reproject(radar['observed'],mask,src_nodata=0,dst_nodata=0,**common)
    rgba=np.zeros((height,width,4),dtype=np.uint8)
    colored=(mask==1)&np.isfinite(target)&(target>=STOPS[0])
    for channel in range(3):
        values=np.interp(np.nan_to_num(target,nan=-999),STOPS,COLORS[:,channel])
        rgba[:,:,channel]=np.where(colored,values,0).astype(np.uint8)
    rgba[:,:,3]=np.where(colored,235,0)
    # A separate mask is essential: blank/no echo must NOT equal no coverage.
    no_data=np.zeros_like(rgba);rows,cols=np.indices((height,width))
    missing=mask==0;hatch=((rows+cols)%16)<3
    no_data[:,:,:3]=[89,105,108]
    no_data[:,:,3]=np.where(missing,np.where(hatch,105,35),0).astype(np.uint8)
    return rgba,no_data,{'width':width,'height':height,'observedFraction':round(float(np.mean(mask==1)),5)}

def atomic_json(target,value):
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w',encoding='utf8',dir=target.parent,delete=False) as fh:
        json.dump(value,fh,ensure_ascii=False,separators=(',',':'));temp=Path(fh.name)
    os.replace(temp,target)

def atomic_png(target,array):
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix='.png',dir=target.parent,delete=False) as fh:temp=Path(fh.name)
    try:Image.fromarray(array).save(temp,format='PNG',optimize=True);os.replace(temp,target)
    finally:temp.unlink(missing_ok=True)

def public_base(value):
    value=value.rstrip('/')
    if value=='/radar-data':return value
    u=urlparse(value)
    if u.scheme!='https' or not u.netloc or u.username or u.password or u.query or u.fragment:raise ValueError('Public base must be /radar-data or an HTTPS directory URL')
    return value

def make_frame(source_file,output,base,now=None):
    radar=read_odim(source_file);now=now or utcnow()
    if radar['time']>now+dt.timedelta(minutes=5):raise ValueError('Future observation rejected')
    if radar['time']<now-dt.timedelta(hours=6):raise ValueError('Observation older than six hours rejected')
    fid=radar['time'].strftime('%Y%m%dT%H%M%SZ')
    rgba,coverage,stats=render_composite(radar)
    atomic_png(output/'frames'/f'{fid}-r.png',rgba)
    atomic_png(output/'frames'/f'{fid}-c.png',coverage)
    frame={'id':fid,'time':iso(radar['time']),'image':f'{base}/frames/{fid}-r.png',
           'coverage':f'{base}/frames/{fid}-c.png','observedFraction':stats['observedFraction'],
           'nativeScaleMeters':radar['nativeScale']}
    atomic_json(output/'frames'/f'{fid}.json',frame)
    return frame

def make_manifest(frames,now=None):
    return {'version':1,'product':'DBZH','unit':'dBZ','projection':'EPSG:3857','bounds':list(BOUNDS),
            'generatedAt':iso(now or utcnow()),'source':'EUMETNET OPERA','license':'CC BY 4.0',
            'sourceUrl':'https://eumetnet.github.io/openradardata-documentation/',
            'licenseUrl':'https://creativecommons.org/licenses/by/4.0/',
            'processing':'Nearest-neighbour reprojection, geographic crop and colour mapping by MeteoNexo; values below 5 dBZ not coloured. Not a forecast.',
            'frames':sorted(frames,key=lambda x:x['time'])}

def publish(output,manifest,bucket,prefix='radar'):
    if not bucket:return
    if not prefix or prefix.strip('/')!=prefix or '..' in prefix:raise ValueError('Use a non-empty safe S3 object prefix')
    import boto3
    client=boto3.client('s3',endpoint_url=os.environ.get('RADAR_S3_ENDPOINT') or None,
                        region_name=os.environ.get('AWS_DEFAULT_REGION','auto'))
    # Images first, manifest last. Readers never see references to incomplete images.
    for frame in manifest['frames']:
        for suffix in ('r','c'):
            name=f"{frame['id']}-{suffix}.png";key=f'{prefix}/frames/{name}'
            try:
                client.head_object(Bucket=bucket,Key=key)
            except client.exceptions.ClientError as exc:
                if str(exc.response.get('Error',{}).get('Code')) not in ('404','NoSuchKey','NotFound'):raise
                client.upload_file(str(output/'frames'/name),bucket,key,ExtraArgs={'ContentType':'image/png','CacheControl':'public, max-age=86400, immutable'})
    client.put_object(Bucket=bucket,Key=f'{prefix}/manifest.json',Body=json.dumps(manifest,separators=(',',':')).encode(),ContentType='application/json',CacheControl='public, max-age=60, must-revalidate')

def clean_local(output,now):
    limit=now-dt.timedelta(hours=24)
    for f in (output/'frames').glob('*'):
        fid=f.name.split('-')[0].split('.')[0]
        if FRAME_RE.fullmatch(fid) and dt.datetime.strptime(fid,'%Y%m%dT%H%M%SZ').replace(tzinfo=UTC)<limit:f.unlink(missing_ok=True)

def cycle(args,s):
    now=utcnow();out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=True)
    base=public_base(args.public_base)
    if args.bucket and base=='/radar-data':raise ValueError('S3 publishing requires an HTTPS public base URL')
    if args.input:
        frames=[make_frame(Path(args.input),out,base,now)]
    else:
        items=select_frames(discover(s,now,args.history),args.max_frames,args.history)
        if not items:raise RuntimeError('No recent OPERA DBZH composites returned by source')
        frames=[]
        for stamp,key in items:
            fid=stamp.strftime('%Y%m%dT%H%M%SZ');cache=out/'frames'/f'{fid}.json'
            try:
                if cache.exists() and (out/'frames'/f'{fid}-r.png').exists() and (out/'frames'/f'{fid}-c.png').exists():
                    frame=json.loads(cache.read_text());frame['image']=f'{base}/frames/{fid}-r.png';frame['coverage']=f'{base}/frames/{fid}-c.png'
                else:
                    payload=request_bytes(s,SOURCE+quote(key,safe='/@'))
                    if not payload.startswith(b'\x89HDF\r\n\x1a\n'):raise ValueError('Source response is not HDF5')
                    with tempfile.NamedTemporaryFile(suffix='.h5') as temp:
                        temp.write(payload);temp.flush();frame=make_frame(Path(temp.name),out,base,now)
                    if frame['id']!=fid:raise ValueError('Observation date differs from source key')
                frames.append(frame)
            except Exception as exc:
                LOG.warning('Frame %s unavailable (%s); no synthetic substitute',fid,type(exc).__name__)
        if not frames:raise RuntimeError('No valid frames processed; previous manifest preserved')
    if len({f['id'] for f in frames})!=len(frames):raise ValueError('Duplicate observation identifiers')
    manifest=make_manifest(frames,now)
    publish(out,manifest,args.bucket,args.prefix)
    atomic_json(out/'manifest.json',manifest)
    atomic_json(out/'health.json',{'ok':True,'checkedAt':iso(now),'latestObservation':frames[-1]['time'],'frames':len(frames)})
    clean_local(out,now)
    LOG.info('Published %s observations; latest %s',len(frames),frames[-1]['time'])
    return manifest

def parser():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',default=os.environ.get('RADAR_DATA_DIR',str(Path(__file__).resolve().parents[1]/'data/radar')))
    p.add_argument('--public-base',default=os.environ.get('RADAR_PUBLIC_BASE_URL','/radar-data'))
    p.add_argument('--bucket',default=os.environ.get('RADAR_S3_BUCKET',''))
    p.add_argument('--prefix',default=os.environ.get('RADAR_S3_PREFIX','radar'))
    p.add_argument('--history',type=int,default=120)
    p.add_argument('--max-frames',type=int,default=13)
    p.add_argument('--loop',type=int,default=0,help='Run sequentially every N seconds (at least 300); 0 = one cycle')
    p.add_argument('--input',help='Process a real local ODIM composite instead of fetching the public source')
    p.add_argument('--probe',action='store_true',help='List current source objects without downloading or publishing')
    return p

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    args=parser().parse_args()
    if not 1<=args.max_frames<=25 or not 30<=args.history<=180 or (args.loop and args.loop<300):raise SystemExit('Invalid history, frame count or update interval')
    if args.input and args.loop:raise SystemExit('--input is only allowed for a single cycle')
    s=session()
    if args.probe:
        rows=select_frames(discover(s,history_minutes=args.history),args.max_frames,args.history)
        print(json.dumps({'source':SOURCE,'observations':[{'time':iso(t),'key':k} for t,k in rows]},indent=2))
        return 0 if rows else 2
    # On a single host/container, prevent overlapping ingestion processes.
    import fcntl
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    with (out/'.worker.lock').open('w') as lock:
        try:fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:LOG.error('Another radar worker is already active');return 3
        while True:
            started=time.monotonic()
            try:cycle(args,s);exit_code=0
            except Exception as exc:
                LOG.error('Radar cycle failed (%s). Last good manifest preserved.',type(exc).__name__)
                atomic_json(out/'health.json',{'ok':False,'checkedAt':iso(utcnow()),'error':type(exc).__name__})
                exit_code=1
            if not args.loop:return exit_code
            time.sleep(max(5,args.loop-(time.monotonic()-started)))

if __name__=='__main__':sys.exit(main())
