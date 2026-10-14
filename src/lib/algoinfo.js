/* Plain-language explanations of each model family, shown under the model picker (ported from feature/admin-login). */

export const ALGO_DESCRIPTIONS = {
  random_forest: `
<div class="algo-rich">
  <div class="algo-rich-header">
    <div class="algo-rich-icon rf"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22v-6M8 22v-4M16 22v-5"/><path d="M12 16a4 4 0 0 0 4-4c0-2.5-2-4-4-4s-4 1.5-4 4a4 4 0 0 0 4 4z"/><path d="M8 18a3 3 0 0 0 3-3c0-2-1.5-3-3-3s-3 1-3 3a3 3 0 0 0 3 3z"/><path d="M16 17a3 3 0 0 0 3-3c0-2-1.5-3-3-3s-3 1-3 3a3 3 0 0 0 3 3z"/></svg></div>
    <div>
      <h4 class="algo-rich-title">Random Forest</h4>
      <p class="algo-rich-subtitle">The "Board of Directors" Approach</p>
    </div>
  </div>
  <div class="algo-rich-body">
    <p>Imagine asking a "Board of Directors" for their opinion on whether a customer will leave. Instead of relying on one person, the algorithm creates hundreds of independent "decision trees" (directors). Each tree looks at a random subset of the customer's data and casts a vote. The final prediction is simply the majority vote.</p>
    <p><strong>Why use it?</strong> It is highly accurate, very stable, and extremely resistant to drawing false conclusions (overfitting). It's the best all-rounder.</p>
  </div>
  <div class="algo-rich-visual">
    <div class="algo-node"><svg viewBox="0 0 24 24" fill="none" stroke="var(--green)"><path d="M12 22V12M12 12L8 8M12 12l4-4"/></svg><span>Tree 1<br>(Votes Churn)</span></div>
    <div class="algo-node"><svg viewBox="0 0 24 24" fill="none" stroke="var(--green)"><path d="M12 22V12M12 12L8 8M12 12l4-4"/></svg><span>Tree 2<br>(Votes Stay)</span></div>
    <div class="algo-node"><svg viewBox="0 0 24 24" fill="none" stroke="var(--green)"><path d="M12 22V12M12 12L8 8M12 12l4-4"/></svg><span>Tree 3<br>(Votes Churn)</span></div>
    <div class="algo-arrow"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M5 12h14M12 5l7 7-7 7"/></svg></div>
    <div class="algo-node"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><rect x="3" y="8" width="18" height="8" rx="2"/></svg><span class="algo-node-result">Result: CHURN<br>(2 vs 1)</span></div>
  </div>
</div>`,

  gradient_boosting: `
<div class="algo-rich">
  <div class="algo-rich-header">
    <div class="algo-rich-icon gb"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"></polyline></svg></div>
    <div>
      <h4 class="algo-rich-title">Gradient Boosting</h4>
      <p class="algo-rich-subtitle">The "Iterative Perfectionist"</p>
    </div>
  </div>
  <div class="algo-rich-body">
    <p>Imagine a team working on a complex puzzle. The first person builds a rough version but makes some mistakes. The next person focuses <em>only</em> on fixing those specific mistakes. The third person fixes the remaining mistakes of the second, and so on. Over time, the team creates a near-perfect result.</p>
    <p><strong>Why use it?</strong> It is incredibly powerful at finding complex, hidden patterns in your data that other algorithms miss. It is often the top-performing model in data science competitions.</p>
  </div>
  <div class="algo-rich-visual">
    <div class="algo-node"><svg viewBox="0 0 24 24" fill="none" stroke="var(--orange)"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg><span>Model 1<br>(High Error)</span></div>
    <div class="algo-arrow"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M5 12h14M12 5l7 7-7 7"/></svg></div>
    <div class="algo-node"><svg viewBox="0 0 24 24" fill="none" stroke="var(--orange)"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg><span>Model 2<br>(Fixes Errors)</span></div>
    <div class="algo-arrow"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M5 12h14M12 5l7 7-7 7"/></svg></div>
    <div class="algo-node"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/></svg><span class="algo-node-result">Final Model<br>(Low Error)</span></div>
  </div>
</div>`,

  logistic_regression: `
<div class="algo-rich">
  <div class="algo-rich-header">
    <div class="algo-rich-icon lr"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 3v18h18"/><path d="M3 21l6-14 4 6 8-10"/></svg></div>
    <div>
      <h4 class="algo-rich-title">Logistic Regression</h4>
      <p class="algo-rich-subtitle">The "Weighing Scale"</p>
    </div>
  </div>
  <div class="algo-rich-body">
    <p>Imagine a simple weighing scale. It looks at risk factors (like "High Cost" or "Many Support Tickets") and places them on the "Churn" side. It places positive factors (like "Long Tenure") on the "Stay" side. If the scale tips past a certain threshold, it predicts the customer will leave.</p>
    <p><strong>Why use it?</strong> It is very fast, transparent, and easy to interpret. You can clearly see exactly how much weight it gave to each specific factor.</p>
  </div>
  <div class="algo-rich-visual">
    <div class="algo-node"><svg viewBox="0 0 24 24" fill="none" stroke="var(--blue)"><rect x="4" y="10" width="4" height="10" rx="1"/><rect x="16" y="6" width="4" height="14" rx="1"/><path d="M4 14h16"/></svg><span>Risk Weights<br>(Added Up)</span></div>
    <div class="algo-arrow"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M5 12h14M12 5l7 7-7 7"/></svg></div>
    <div class="algo-node"><svg viewBox="0 0 24 24" fill="none" stroke="var(--blue)"><circle cx="12" cy="12" r="10"/><line x1="12" y1="2" x2="12" y2="22"/></svg><span>Threshold<br>(e.g. 50%)</span></div>
    <div class="algo-arrow"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M5 12h14M12 5l7 7-7 7"/></svg></div>
    <div class="algo-node"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg><span class="algo-node-result">Probability<br>of Churn</span></div>
  </div>
</div>`
};
