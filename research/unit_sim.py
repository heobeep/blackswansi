import sys; import os; sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import numpy as np, pandas as pd
import app.portfolio_backtest as pb
from app.portfolio_backtest import *

def unit_sim(close, cfg, trim_band=0.30, add_band=0.30, cost=0.0015, start="2018-06-01"):
    """Daily loop that mirrors PortfolioEngine._rebalance with REAL position drift (units held)."""
    rets=close.pct_change().fillna(0.0)
    w=raw_weights(close,cfg)
    if cfg.vol_target: w=w.mul(pb._vol_scale(w,rets,cfg),axis=0)
    W=w.to_numpy(); C=close.to_numpy(); n=C.shape[1]
    units=np.zeros(n); cash=1.0; eq=[]; dates=[]
    i0=close.index.get_loc(close.loc[start:].index[0])
    for i in range(i0,len(close)):
        px=np.nan_to_num(C[i]); equity=cash+float((units*px).sum())
        # decisions at day i-1 close, executed at that close, then earn day-i return (same convention as run_portfolio)
        if i>i0:
            py=np.nan_to_num(C[i-1]); eq_y=cash+float((units*py).sum()); tgt=W[i-1]*eq_y; val=units*py
            # exits
            for j in range(n):
                if units[j]>0 and tgt[j]<=0:
                    cash+=val[j]*(1-cost); units[j]=0
            val=units*py
            # trims
            for j in range(n):
                if units[j]>0 and trim_band is not None and val[j]>tgt[j]*(1+trim_band):
                    sell=val[j]-tgt[j]; cash+=sell*(1-cost); units[j]-=sell/py[j]
            val=units*py
            # entries / top-ups
            for j in np.argsort(-tgt):
                if tgt[j]<=0 or py[j]<=0: continue
                have=val[j]
                if units[j]>0 and have>=tgt[j]*(1-add_band): continue
                spend=min(tgt[j]-have,cash*0.98)
                if spend>1e-6:
                    cash-=spend; units[j]+=spend*(1-cost)/py[j]
        equity=cash+float((units*np.nan_to_num(C[i])).sum()); eq.append(equity); dates.append(close.index[i])
    s=pd.Series(eq,index=dates); return s.pct_change().fillna(0.0)

if __name__=="__main__":
    SYMS=["BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT","XRPUSDT","ADAUSDT","DOGEUSDT","AVAXUSDT","LINKUSDT","DOTUSDT"]
    close=load_closes("data/csv",SYMS)
    cfg=PCfg(trend_len=50,vol_target=0.30,rebalance_days=1,signal="blend")
    V={"V0 current: trim winners >30% over target":dict(trim_band=0.30),
       "V1 trim only if >100% over target":dict(trim_band=1.0),
       "V2 never trim winners (let them run)":dict(trim_band=None)}
    print(f"{'':44}| IS(18-22) sh calmar | OOS(23+) cagr sh calmar dd | ex-2021 cagr sh | FULL cagr sh dd | worst yr")
    for n,kw in V.items():
        net=unit_sim(close,cfg,**kw)
        i=metrics(net.loc[:"2022-12-31"]);o=metrics(net.loc["2023-01-01":]);e=metrics(net[net.index.year!=2021]);f=metrics(net)
        yrs=[((1+net[str(y)]).prod()-1)*100 for y in range(2018,2027)]
        print(f"{n:44}| {i['sharpe']:4.2f} {i['calmar']:5.2f} | {o['cagr_pct']:+4.0f}% {o['sharpe']:4.2f} {o['calmar']:5.2f} {o['maxdd_pct']:4.0f}% | {e['cagr_pct']:+4.0f}% {e['sharpe']:4.2f} | {f['cagr_pct']:+4.0f}% {f['sharpe']:4.2f} {f['maxdd_pct']:4.0f}% | {min(yrs):+.0f}%")
