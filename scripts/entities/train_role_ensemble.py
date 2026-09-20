#!/usr/bin/env python3
"""train_role_ensemble.py — Train full XGBoost ensemble for role classification.

Run: nohup .venv/bin/python3 -u scripts/entities/train_role_ensemble.py \
         > data/sync/ensemble-train-$(date +%%Y%%m%%d-%%H%%M).log 2>&1 &
"""

def main():
    import sys, os, re, logging, pickle, json, time
    ROOT = os.path.join(os.path.dirname(__file__), "..", "..")
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    log = logging.getLogger("ensemble")
    log.info("Starting ensemble training...")

    import numpy as np
    import fasttext
    import xgboost as xgb
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score
    from sentence_transformers import SentenceTransformer
    from db import get_engine
    from sqlalchemy import text as sql_text

    ROLES = ["applicant","attorney","staff","owner","presenter","reference","organization","mentioned","iga_counterparty","representative"]
    LM = {n:i for i,n in enumerate(ROLES)}
    BG = {"phoenix-cc":0,"tempe-cc":0,"chandler-cc":0,"scottsdale-cc":0,"mesa-city-council":0,"glendale-cc":0,"goodyear-cc":0,"gilbert-cc":0,"bos":1,"pz":2,"phoenix-pc":2,"phoenix-ti":3,"phoenix-ps":3,"phoenix-ed":3,"phoenix-boa":4,"scottsdale-boa":4}

    log.info("Loading data from DB...")
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(sql_text("""
            SELECT e.name, em.context_snippet, em.role_in_context, ai.body, ai.agenda_item_text
            FROM entity_mentions em JOIN entities e ON em.entity_id = e.id
            JOIN agenda_items ai ON ai.id = CAST(em.source_id AS INTEGER)
            WHERE em.role_in_context IS NOT NULL AND em.role_in_context != ''
              AND LENGTH(em.context_snippet) > 10 LIMIT 40000
        """)).fetchall()

    names, ctxs, roles, bodies, fulls = [], [], [], [], []
    for r in rows:
        lid = LM.get(r[2], -1)
        if lid >= 0:
            names.append(r[0] or ""); ctxs.append(r[1] or "")
            roles.append(lid); bodies.append(r[3] or ""); fulls.append(r[4] or "")
    log.info("Loaded %d examples", len(roles))

    # Split
    tr, te = train_test_split(range(len(roles)), test_size=0.2, random_state=42, stratify=roles)
    def g(a,i): return [a[j] for j in i]

    # Signal 1: fastText probabilities
    log.info("fastText probs...")
    ft_model = fasttext.load_model(os.path.join(ROOT, "data", "role_classifier.bin"))
    def get_ft(arr_n, arr_c):
        res = np.zeros((len(arr_n), len(ROLES)))
        for i in range(len(arr_n)):
            txt = (arr_c[i] + " " + arr_n[i]).replace("\n", " ").replace("\r", " ")
            if not txt.strip(): continue
            p = ft_model.predict(txt, k=len(ROLES))
            for j, lb in enumerate(p[0]):
                lid = LM.get(lb.replace("__label__",""), -1)
                if lid >= 0: res[i, lid] = float(p[1][j])
        return res
    train_ft = get_ft(g(names, tr), g(ctxs, tr))
    test_ft = get_ft(g(names, te), g(ctxs, te))
    log.info("  fastText done")

    # Signal 2: sentence embeddings
    log.info("Sentence embeddings...")
    enc = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    train_ne = enc.encode(g(names, tr), show_progress_bar=True)
    test_ne = enc.encode(g(names, te), show_progress_bar=True)
    log.info("  name embeddings done")

    # Context window embeddings
    def ctx_window(full, name, w=5):
        if not full or not name: return name or ""
        i = full.lower().find(name.lower()[:30])
        if i < 0: return name
        return full[max(0,i-w*2):min(len(full),i+len(name)+w*20)]
    train_ctx = [ctx_window(g(fulls,tr)[i], g(names,tr)[i]) for i in range(len(g(names,tr)))]
    test_ctx = [ctx_window(g(fulls,te)[i], g(names,te)[i]) for i in range(len(g(names,te)))]
    train_ce = enc.encode(train_ctx, show_progress_bar=True)
    test_ce = enc.encode(test_ctx, show_progress_bar=True)

    # Signal 3: TF-IDF on context window
    log.info("TF-IDF...")
    tfidf = TfidfVectorizer(max_features=500, ngram_range=(1,2), token_pattern=r"(?u)\b\w+\b")
    train_tf = tfidf.fit_transform(train_ctx).toarray()
    test_tf = tfidf.transform(test_ctx).toarray()

    # Signal 4: surface form features
    log.info("Surface features...")
    def surf(name, ctx):
        n, c = name.lower(), ctx.lower()
        return [len(name), len(name.split()),
                1 if name.isupper() and len(name)>3 else 0,
                1 if name and name[0].isupper() else 0,
                1 if "&" in n or " AND " in name else 0,
                1 if "," in name else 0, name.count(","),
                1 if any(w in n for w in ["attorney","esq","llc","inc","corp","plc","ltd"]) else 0,
                1 if any(w in n for w in ["jr","sr","iii","phd","md","jd"]) else 0,
                1 if any(w in n for w in ["city of","county of","town of","arizona","state of"]) else 0,
                1 if any(w in c for w in ["applicant","attorney","representative","staff"]) else 0,
                1 if name.endswith(".") else 0, 1 if "." in name else 0]
    train_sf = np.array([surf(g(names,tr)[i], g(ctxs,tr)[i]) for i in range(len(g(names,tr)))])
    test_sf = np.array([surf(g(names,te)[i], g(ctxs,te)[i]) for i in range(len(g(names,te)))])

    # Signal 5: body group
    log.info("Body features...")
    def bf(arr):
        f = np.zeros((len(arr), 6))
        for i, b in enumerate(arr): f[i, BG.get(b, 5)] = 1
        return f
    train_body = bf(g(bodies, tr))
    test_body = bf(g(bodies, te))

    # Combine
    log.info("Combining...")
    train_X = np.hstack([train_ft, train_ne, train_ce, train_tf, train_sf, train_body])
    test_X = np.hstack([test_ft, test_ne, test_ce, test_tf, test_sf, test_body])
    log.info("Train: %s  Test: %s", train_X.shape, test_X.shape)

    # Train XGBoost
    log.info("Training XGBoost...")
    dtrain = xgb.DMatrix(train_X, label=np.array(roles)[tr])
    dtest = xgb.DMatrix(test_X, label=np.array(roles)[te])
    model = xgb.train({"objective":"multi:softprob", "num_class":len(ROLES),
        "max_depth":8, "eta":0.1, "subsample":0.8, "colsample_bytree":0.8,
        "eval_metric":"mlogloss", "seed":42},
        dtrain, num_boost_round=200, evals=[(dtest,"test")], verbose_eval=50)

    # Evaluate
    preds = model.predict(dtest)
    pred_lb = np.argmax(preds, axis=1)
    acc = accuracy_score(np.array(roles)[te], pred_lb)
    log.info("\n=== RESULTS ===")
    log.info("XGBoost ensemble: %.3f (%d/%d)", acc, int(acc*len(pred_lb)), len(pred_lb))

    # Save
    log.info("Saving...")
    model.save_model(os.path.join(ROOT, "data", "role_ensemble_xgb.json"))
    with open(os.path.join(ROOT, "data", "role_ensemble_tfidf.pkl"), "wb") as f: pickle.dump(tfidf, f)
    with open(os.path.join(ROOT, "data", "role_ensemble_labels.pkl"), "wb") as f: pickle.dump(ROLES, f)
    log.info("Saved: role_ensemble_xgb.json + role_ensemble_tfidf.pkl + role_ensemble_labels.pkl")
    log.info("Done")

if __name__ == "__main__":
    main()
