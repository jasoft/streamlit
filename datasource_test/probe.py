import akshare as ak, efinance as ef, adata, qstock, warnings
warnings.filterwarnings('ignore')

fut = [n for n in dir(ak) if 'futures' in n and ('spot' in n or 'realtime' in n or 'zh' in n)]
print("AKSHARE futures:", fut)
opt = [n for n in dir(ak) if n.startswith('option') and ('sina' in n or 'em' in n or 'spot' in n or 'current' in n or 'code' in n)]
print("AKSHARE options:", opt)
print("EFINANCE:", [n for n in dir(ef) if not n.startswith('_')])
print("ADATA:", [n for n in dir(adata) if not n.startswith('_')])
fd = [n for n in dir(adata.futures) if not n.startswith('_')] if hasattr(adata,'futures') else 'none'
print("ADATA.futures:", fd)
print("QSTOCK:", [n for n in dir(qstock) if not n.startswith('_')])
