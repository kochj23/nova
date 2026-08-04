#!/opt/homebrew/bin/python3
"""Backfill cover images on operations articles that lack one. Generates an image from
each article's title/description, converts to webp, inserts the Hugo `cover:` frontmatter,
then commits + pushes. Must run under launchd (Hugo repo on FDA-blocked /Volumes/Data)."""
import os, glob, re, subprocess, time, shutil, sys
sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
from nova_image_utils import generate_image

REPO=os.path.expanduser("~/nova-journal")
CONTENT=f"{REPO}/content/operations"
IMGDIR=f"{REPO}/static/images/operations"
LOG=os.path.expanduser("~/.openclaw/logs/ops_image_backfill.log")
def log(m): open(LOG,"a").write(f"[{time.strftime('%H:%M:%S')}] {m}\n")

def frontmatter(txt):
    if txt.count("---")<2: return "", txt
    _,fm,body = txt.split("---",2); return fm, body

os.makedirs(IMGDIR, exist_ok=True)
fixed=[]
for f in sorted(glob.glob(CONTENT+"/*.md")):
    base=os.path.basename(f)
    if base=="_index.md": continue
    txt=open(f,errors="replace").read()
    fm,body=frontmatter(txt)
    if ("cover:" in fm and "image:" in fm) or re.search(r'!\[.*\]\(',txt):
        continue  # already has an image
    slug=base[:-3]
    title=(re.search(r'title:\s*"?(.*?)"?\s*$',fm,re.M) or [None,slug])[1]
    desc=(re.search(r'description:\s*"?(.*?)"?\s*$',fm,re.M) or [None,""])[1]
    prompt=(f"Editorial cover illustration for a tech/operations article titled '{title}'. "
            f"{desc}. Clean modern digital art, conceptual, no text, no words.")
    log(f"generating image for {base} …")
    img=generate_image(prompt, width=1024, height=768, section="operations")
    if not img or not os.path.exists(img):
        log(f"  image FAILED for {base}"); continue
    dest=f"{IMGDIR}/{slug}.webp"
    try:
        r=subprocess.run(["cwebp","-q","82","-resize","1200","0",img,"-o",dest],capture_output=True,timeout=40)
        if r.returncode!=0 or not os.path.exists(dest): shutil.copy2(img,dest)
    except Exception:
        shutil.copy2(img,dest)
    cover=(f'cover:\n  image: "/images/operations/{slug}.webp"\n'
           f'  alt: "{title[:80]}"\n  relative: false\n')
    new_fm = fm.rstrip("\n")+"\n"+cover
    open(f,"w").write("---"+new_fm+"---"+body)
    fixed.append(base); log(f"  DONE {base}")

if fixed:
    subprocess.run(["git","add","-A"],cwd=REPO,capture_output=True,timeout=30)
    subprocess.run(["git","commit","-m",f"ops: backfill cover images on {len(fixed)} articles"],cwd=REPO,capture_output=True,text=True,timeout=30)
    # Rebase onto origin BEFORE pushing so a diverged clone can't silently strand commits.
    pull=subprocess.run(["git","pull","--rebase","--autostash","origin","main"],cwd=REPO,capture_output=True,text=True,timeout=180)
    if pull.returncode!=0:
        subprocess.run(["git","rebase","--abort"],cwd=REPO,capture_output=True,timeout=30)
        log(f"push ABORTED — pull --rebase failed (diverged/conflict): {pull.stderr.strip()[:200]}")
    else:
        p=subprocess.run(["git","push"],cwd=REPO,capture_output=True,text=True,timeout=90)
        log(f"pushed: rc={p.returncode} {(p.stdout+p.stderr).strip()[:150]}")
log(f"BACKFILL DONE — fixed {len(fixed)}: {fixed}")
