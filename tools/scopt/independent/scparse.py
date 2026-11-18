import struct, sys, collections
FNV_OFF=1469598103934665603; FNV_P=1099511628211; M=(1<<64)-1
def fnv(b,h=FNV_OFF):
    for x in b: h=((h^x)*FNV_P)&M
    return h
path=sys.argv[1]
f=open(path,'rb')
hdr=f.read(80)
magic,ver,hsz,code_size,code_addr,prol_end,rcount,sig,hps,tpb,tbsz,res,ck=struct.unpack('<8sIIQQQQQIIIIQ',hdr)
print('magic',magic,'ver',ver,'hsz',hsz,'code_size',hex(code_size),'code_addr',hex(code_addr),'prologue_end',hex(prol_end),'records',rcount,'sig',hex(sig),'host_page',hps,'target_page_bits',tpb,'tb_size',tbsz,'hdr_ck_ok',fnv(hdr[:72])==ck)
roff=0x10000+code_size
f.seek(roff); data=f.read()
print('record bytes',len(data),'n',len(data)//104)
recs=[]
for i in range(len(data)//104):
    r=data[i*104:(i+1)*104]
    (tb,end,tc,tcs,pc,csb,pa0,pa1,gh,fl,cf,gs,ic,jr0,jr1,ji0,ji1,cks,com)=struct.unpack('<QQQQQQQQQIIHHHHHHII4x',r)
    h=fnv(r[:92]); ok=((h^(h>>32))&0xffffffff)==cks and com==0x53435231
    recs.append(dict(i=i,tb=tb,end=end,tc=tc,tcs=tcs,pc=pc,cs=csb,pa0=pa0,pa1=pa1,fl=fl,cf=cf,gs=gs,ic=ic,jr=(jr0,jr1),ji=(ji0,ji1),ok=ok))
bad=[r['i'] for r in recs if not r['ok']]
print('bad records',bad)
print('tc-tb deltas',collections.Counter(r['tc']-r['tb'] for r in recs))
print('tb_offset mod 64/128',collections.Counter(r['tb']%128 for r in recs))
print('sorted by tc',all(recs[i]['tc']<recs[i+1]['tc'] for i in range(len(recs)-1)),'sorted by tb',all(recs[i]['tb']<recs[i+1]['tb'] for i in range(len(recs)-1)))
print('cflags',collections.Counter(hex(r['cf']) for r in recs))
print('flags',collections.Counter(hex(r['fl']) for r in recs))
print('cs_base',collections.Counter(hex(r['cs']) for r in recs))
print('pc values',collections.Counter(hex(r['pc']) for r in recs).most_common(5))
print('pa1 != -1',sum(1 for r in recs if r['pa1']!=M))
keys=collections.Counter((r['pa0'],r['fl'],r['cs'],r['cf']) for r in recs)
print('dup keys',sum(1 for k,v in keys.items() if v>1))
print('jmp_reset',collections.Counter((r['jr'][0]!=0xffff, r['jr'][1]!=0xffff) for r in recs))
print('max_end',hex(max(r['end'] for r in recs)),'min tb',hex(min(r['tb'] for r in recs)))
print('pa0 range',hex(min(r['pa0'] for r in recs)),hex(max(r['pa0'] for r in recs)))
print('icount max',max(r['ic'] for r in recs),'tcs max',max(r['tcs'] for r in recs))
# gaps
s=sorted(recs,key=lambda r:r['tb'])
gaps=[(hex(a['end']),hex(b['tb'])) for a,b in zip(s,s[1:]) if b['tb']-a['end']>=128]
print('gaps>=128',gaps[:10],len(gaps))
import pickle; pickle.dump((code_addr,prol_end,recs),open(sys.argv[2],'wb'))
