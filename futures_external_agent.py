"""Optional untrusted HTTPS decision source for the Futures PAPER operator."""
import json
import math
import os
import urllib.request


ACTIONS = frozenset({'OPEN_LONG','OPEN_SHORT','KEEP_LONG','KEEP_SHORT',
    'CLOSE_LONG','CLOSE_SHORT','REDUCE_LONG','REDUCE_SHORT','HEDGE','FLAT'})
FIELDS = frozenset({'action','symbol','positionSide','desiredSize','stop','target',
    'hedgeRatio','confidence','probabilityNetProfit','expectedNetReturn',
    'timeHorizon','reasonCodes'})


def validate_decision(value):
    if not isinstance(value,dict) or set(value)-FIELDS or value.get('action') not in ACTIONS:
        return None
    action=value['action']
    symbol=value.get('symbol')
    if action!='FLAT' and (not isinstance(symbol,str) or len(symbol)>24):
        return None
    if symbol is not None and not isinstance(symbol,str):
        return None
    side=value.get('positionSide')
    if action.endswith('LONG') and side!='LONG':
        return None
    if action.endswith('SHORT') and side!='SHORT':
        return None
    if action=='HEDGE' and side not in ('LONG','SHORT'):
        return None
    for key in ('desiredSize','stop','target','hedgeRatio','confidence',
                'probabilityNetProfit','expectedNetReturn','timeHorizon'):
        item=value.get(key)
        if item is not None and (isinstance(item,bool) or not isinstance(item,(int,float)) or
                                 not math.isfinite(item)):
            return None
    reasons=value.get('reasonCodes',[])
    if not isinstance(reasons,list) or len(reasons)>20 or any(
            not isinstance(item,str) or len(item)>100 for item in reasons):
        return None
    if action.startswith('OPEN_') or action=='HEDGE':
        if value.get('desiredSize') is None or value['desiredSize']<=0:
            return None
        if value.get('stop') is None or value.get('target') is None:
            return None
    if action.startswith('REDUCE_') and value.get('desiredSize') not in (.25,.5,.75):
        return None
    return value


def external_decision(payload, opener=None):
    if os.environ.get('AGENT_PROVIDER','LOCAL').upper()!='EXTERNAL':
        return None
    url=os.environ.get('AGENT_API_URL','')
    key=os.environ.get('AGENT_API_KEY','')
    if not url.startswith('https://') or not key:
        return None
    try:
        timeout=max(1.,min(15.,float(os.environ.get('AGENT_TIMEOUT','4'))))
    except ValueError:
        timeout=4.
    request=urllib.request.Request(url,json.dumps(payload,allow_nan=False).encode(),
        method='POST',headers={'Content-Type':'application/json',
                               'Authorization':'Bearer '+key})
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,request,fp,code,msg,headers,newurl):
            return None
    try:
        with (opener or urllib.request.build_opener(NoRedirect).open)(request,timeout=timeout) as response:
            if response.status!=200:
                return None
            body=response.read(65537)
            if len(body)>65536:
                return None
            return validate_decision(json.loads(body,parse_constant=lambda _: (_ for _ in ()).throw(ValueError())))
    except (OSError,ValueError,TimeoutError,TypeError):
        return None
