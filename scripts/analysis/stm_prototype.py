#!/usr/bin/env python3
"""
STM Prototype: Does Chandler talk about different things than Tempe?

Fits a Structural Topic Model over agenda item titles + text from Chandler
and Tempe (2021-2025), using jurisdiction as a prevalence covariate to
estimate which topics shift between the two cities.

Usage:
    cd /path/to/poliscopic
    source .venv/bin/activate
    python3 scripts/analysis/stm_prototype.py

Requirements: structural-topic-model, scikit-learn, pandas, sqlalchemy, psycopg2-binary
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import numpy as np
import pandas as pd
from sklearn.feature_extraction.text import CountVectorizer

from db import get_session
from db.models import AgendaItem, Meeting, PublicBody, Jurisdiction

# ── 1. Load data ──────────────────────────────────────────────────────────────

def load_data(jurisdictions=('City of Chandler', 'City of Tempe'),
              year_min=2021, year_max=2025):
    """Fetch agenda items with titles from the two cities."""
    s = get_session()
    rows = (
        s.query(
            Jurisdiction.name.label('jurisdiction'),
            PublicBody.name.label('body'),
            Meeting.meeting_date,
            AgendaItem.agenda_item_title,
            AgendaItem.agenda_item_text,
        )
        .join(Meeting, AgendaItem.meeting_db_id == Meeting.id)
        .join(PublicBody, Meeting.public_body_id == PublicBody.id)
        .join(Jurisdiction, PublicBody.jurisdiction_id == Jurisdiction.id)
        .filter(Jurisdiction.name.in_(jurisdictions))
        .filter(Meeting.meeting_date >= f'{year_min}-01-01')
        .filter(Meeting.meeting_date <= f'{year_max}-12-31')
        .filter(AgendaItem.agenda_item_title != None)
        .filter(AgendaItem.agenda_item_title != '')
    )
    df = pd.read_sql(rows.statement, s.bind)
    s.close()

    # Build a combined text field — title + body text if available
    df['text'] = df['agenda_item_title'].fillna('')
    has_body = df['agenda_item_text'].notna() & (df['agenda_item_text'] != '')
    df.loc[has_body, 'text'] = df.loc[has_body, 'text'] + ' ' + df.loc[has_body, 'agenda_item_text']

    # Extract year
    df['year'] = pd.to_numeric(df['meeting_date'].str[:4], errors='coerce').fillna(0).astype(int)

    print(f"Loaded {len(df)} agenda items")
    for j in jurisdictions:
        print(f"  {j}: {len(df[df['jurisdiction'] == j])}")
    print(f"  Date range: {df['meeting_date'].min()} to {df['meeting_date'].max()}")
    print(f"  Bodies: {df['body'].value_counts().to_dict()}")
    print(f"  With body text: {has_body.sum()}")
    return df


# ── 2. Prepare design matrices ────────────────────────────────────────────────

def prepare_matrices(df):
    """Vectorize text and build prevalence covariate matrix."""
    texts = df['text'].tolist()

    # Vectorizer: unigrams + bigrams, filter extremes
    vectorizer = CountVectorizer(
        lowercase=True,
        strip_accents='unicode',
        stop_words='english',
        ngram_range=(1, 2),
        min_df=5,
        max_df=0.8,
        max_features=5000,
    )
    X = vectorizer.fit_transform(texts)

    # Remove empty documents (all terms removed by min_df or stopwords)
    nonzero_counts = X.sum(axis=1).A1
    empty_mask = nonzero_counts == 0
    if empty_mask.sum() > 0:
        print(f"  Dropping {empty_mask.sum()} empty documents...")
        X = X[~empty_mask]
        df = df.iloc[~empty_mask].reset_index(drop=True)

    print(f"\nVocabulary size: {X.shape[1]}")
    print(f"  (after filtering: {X.shape[0]} docs × {X.shape[1]} terms)")

    # Prevalence covariates: jurisdiction + body + year
    # Create a design matrix with:
    #   - intercept (added automatically by the model)
    #   - jurisdiction: City of Chandler = 1, City of Tempe = 0
    #   - body: one-hot (drop first to avoid collinearity)
    #   - year: numeric

    covar = pd.DataFrame(index=df.index)
    covar['jurisdiction_chandler'] = (df['jurisdiction'] == 'City of Chandler').astype(float)

    # Body one-hot: only keep bodies with enough docs
    body_counts = df['body'].value_counts()
    major_bodies = body_counts[body_counts >= 50].index.tolist()
    for b in major_bodies:
        covar[f'body_{b.replace(" ", "_")}'] = (df['body'] == b).astype(float)

    # Year (centered for interpretability)
    covar['year_centered'] = df['year'] - df['year'].median()

    print(f"\nPrevalence covariates ({len(covar.columns)}):")
    for c in covar.columns:
        print(f"  {c}: [{covar[c].min()}, {covar[c].max()}]")

    # Make sure index is clean
    covar.reset_index(drop=True, inplace=True)
    df.reset_index(drop=True, inplace=True)

    return X, covar, vectorizer, df


# ── 3. Fit & Report ───────────────────────────────────────────────────────────

def run_model(X, prevalence, K=12):
    """Fit STM and return model."""
    from stm import StructuralTopicModel

    model = StructuralTopicModel(
        n_components=K,
        init='spectral',
        max_iter=200,
        tol=1e-5,
        sigma_prior=0.1,
        random_state=42,
        verbose=True,
    )

    print(f"\nFitting STM with K={K} topics...")
    model.fit(X, prevalence=prevalence.values)
    print(f"  Converged: {model.converged_}")
    print(f"  Iterations: {model.n_iter_}")
    print(f"  Lower bound: {model.bound_[-1]:.2f}")
    return model


def report_results(model, vectorizer, prevalence, df=None):
    """Print interpretable results."""
    K = model.n_components_
    feature_names = vectorizer.get_feature_names_out()
    covar_names = prevalence.columns.tolist()

    print(f"\n{'='*70}")
    print(f"TOPIC SUMMARY (K={K})")
    print(f"{'='*70}")

    # Top words per topic
    for k in range(K):
        # Get top words by probability
        topic_dist = model.components_[k]
        top_indices = np.argsort(topic_dist)[-15:][::-1]
        top_words = [feature_names[i] for i in top_indices]
        top_probs = [topic_dist[i] for i in top_indices]

        # Get top words by FREX (frequency-exclusivity)
        # Compute exclusivity: how unique each word is to this topic
        # FREX = harmonic mean of (prob within topic / sum across topics) and probability
        word_weights = model.components_ / (model.components_.sum(axis=0) + 1e-10)
        exclusivity = word_weights[k]
        frex = 2 / (1/topic_dist + 1/exclusivity)  # simplified FREX with w=0.5

        frex_indices = np.argsort(frex)[-10:][::-1]
        frex_words = [feature_names[i] for i in frex_indices]

        # Print topic k
        top_words_str = ' | '.join(f'{w} ({p:.4f})' for w, p in zip(top_words[:8], top_probs[:8]))
        frex_str = ' | '.join(frex_words[:8])
        print(f"\n── Topic {k} ──")
        print(f"  Top words:      {top_words_str}")
        print(f"  FREX words:     {frex_str}")

    # Gamma coefficients: jurisdiction effect per topic
    print(f"\n{'='*70}")
    print(f"JURISDICTION EFFECT (Chandler vs Tempe)")
    print(f"{'='*70}")
    print("Positive = Chandler talks about this topic MORE")
    print("Negative = Chandler talks about this topic LESS")
    print(f"{'-'*70}")

    # model.gamma_ is (K-1, n_covariates) — one topic is the reference category.
    # Column 0 is intercept. We want the coefficient for jurisdiction_chandler.
    j_idx = covar_names.index('jurisdiction_chandler')
    n_prevalence = model.gamma_.shape[0]  # K-1
    gamma_j = model.gamma_[:, j_idx]

    # Sort by absolute effect
    sorted_idx = np.argsort(np.abs(gamma_j))[::-1]

    # Top words for reference
    feature_names = vectorizer.get_feature_names_out()

    for gamma_idx in sorted_idx:
        # The reference topic (topic K-1) has gamma=0 by definition
        topic_dist = model.components_[gamma_idx]
        top_indices = np.argsort(topic_dist)[-5:][::-1]
        top_words = [feature_names[i] for i in top_indices]
        direction = "↑ Chandler" if gamma_j[gamma_idx] > 0 else "↓ Chandler"
        print(f"  Topic {gamma_idx:2d} [{direction}]  gamma={gamma_j[gamma_idx]:+.4f}")
        print(f"    Words: {' | '.join(top_words)}")
    print(f"  Topic {n_prevalence:2d} [reference category — gamma fixed at 0]")
    # Show the reference topic's words
    ref_dist = model.components_[n_prevalence]
    ref_indices = np.argsort(ref_dist)[-5:][::-1]
    ref_words = [feature_names[i] for i in ref_indices]
    print(f"    Words: {' | '.join(ref_words)}")

    # Body effects (if any)
    body_covars = [c for c in covar_names if c.startswith('body_')]
    if body_covars:
        print(f"\n{'='*70}")
        print(f"BODY EFFECTS (top body covariates per topic)")
        print(f"{'='*70}")
        n_prevalence = model.gamma_.shape[0]
        for k in range(n_prevalence):
            body_effects = []
            for c in body_covars:
                c_idx = covar_names.index(c)
                eff = model.gamma_[k, c_idx]
                if abs(eff) > 0.1:  # threshold for reporting
                    body_effects.append((c, eff))
            if body_effects:
                body_effects.sort(key=lambda x: abs(x[1]), reverse=True)
                label = ' | '.join(f"{c.replace('body_', '')}: {e:+.3f}" for c, e in body_effects[:3])
                print(f"  Topic {k}: {label}")

    # Topic prevalence by city (using theta_ if available)
    if hasattr(model, 'theta_') and model.theta_ is not None and len(df) == model.theta_.shape[0]:
        print(f"\n{'='*70}")
        print(f"TOPIC PREVALENCE BY CITY")
        print(f"{'='*70}")
        theta = model.theta_
        n_topics = theta.shape[1]
        for city_name in ['City of Chandler', 'City of Tempe']:
            city_mask = df['jurisdiction'] == city_name
            if city_mask.sum() == 0:
                continue
            city_theta = theta[city_mask.values]
            mean_prevalence = city_theta.mean(axis=0)
            top_topics = np.argsort(mean_prevalence)[::-1][:5]
            topic_list = ', '.join(f'T{k}: {mean_prevalence[k]:.1%}' for k in top_topics)
            print(f"{city_name} topic prevalence:")
            for k in top_topics:
                print(f'  T{k}: {mean_prevalence[k]:.1%}')
        print()

    # Topic-topic correlation (which topics co-occur)
    print(f"\n{'='*70}")
    print(f"TOPIC-TOPIC CORRELATIONS (|r| > 0.3 shown)")
    print(f"{'='*70}")
    if hasattr(model, 'theta_') and model.theta_ is not None:
        theta = model.theta_
        n_topics = theta.shape[1]
        for i in range(n_topics):
            for j in range(i+1, n_topics):
                corr_val = np.corrcoef(theta[:, i], theta[:, j])[0, 1]
                if abs(corr_val) > 0.3:
                    arrow = "↗" if corr_val > 0 else "↘"
                    print(f"  Topic {i} {arrow} Topic {j}:  r={corr_val:.3f}")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    df = load_data()
    X, prevalence, vectorizer, df = prepare_matrices(df)
    model = run_model(X, prevalence, K=12)
    report_results(model, vectorizer, prevalence, df=df)

    print(f"\n{'='*70}")
    print("DONE")
    print(f"{'='*70}")
