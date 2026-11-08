import React, { useState, useEffect } from 'react';

type FormState = 'idle' | 'submitting' | 'success' | 'already_subscribed' | 'error';

interface ApiResponse {
  status: string;
  message: string;
  id?: number;
  email?: string;
  sku?: string;
  detail?: string;
}

export const App: React.FC = () => {
  const [sku, setSku] = useState('');
  const [email, setEmail] = useState('');
  const [skuPrefilled, setSkuPrefilled] = useState(false);
  const [state, setState] = useState<FormState>('idle');
  const [responseMessage, setResponseMessage] = useState('');
  const [fieldErrors, setFieldErrors] = useState<{ email?: string; sku?: string }>({});

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const paramSku = params.get('sku');
    if (paramSku && paramSku.trim()) {
      setSku(paramSku.trim());
      setSkuPrefilled(true);
    }
  }, []);

  const validate = (): boolean => {
    const errors: { email?: string; sku?: string } = {};

    if (!sku.trim()) {
      errors.sku = 'Please provide a product SKU.';
    }

    if (!email.trim()) {
      errors.email = 'Please provide your email address.';
    } else {
      const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
      if (!emailRegex.test(email.trim())) {
        errors.email = 'Please enter a valid email address.';
      }
    }

    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!validate()) return;

    setState('submitting');
    setFieldErrors({});

    try {
      const response = await fetch('/api/requests', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
        },
        body: JSON.stringify({
          email: email.trim(),
          sku: sku.trim(),
        }),
      });

      const data: ApiResponse = await response.json();

      if (response.status === 201) {
        setState('success');
        setResponseMessage(data.message || "You're subscribed! We will notify you when this item is back in stock.");
      } else if (response.status === 200 && data.status === 'already_subscribed') {
        setState('already_subscribed');
        setResponseMessage(data.message || "You are already on the notification list for this item.");
      } else {
        setState('error');
        setResponseMessage(data.detail || data.message || 'Unable to complete your request. Please try again.');
      }
    } catch {
      setState('error');
      setResponseMessage('Network connection failed. Please check your connection and try again.');
    }
  };

  const handleReset = () => {
    setState('idle');
    setEmail('');
    setFieldErrors({});
    setResponseMessage('');
  };

  return (
    <div className="embed-container">
      <header className="embed-header">
        <h1 className="embed-title">Back in Stock Notification</h1>
        <p className="embed-description">
          Leave your email and we will notify you immediately when inventory arrives.
        </p>
        {skuPrefilled && (
          <div className="sku-badge-row">
            <span className="sku-badge-label">Item:</span>
            <span className="sku-badge">{sku}</span>
          </div>
        )}
      </header>

      {state === 'success' && (
        <div className="state-message success" role="status">
          <div className="state-message-title">Subscription confirmed</div>
          <div>{responseMessage}</div>
          <button type="button" className="reset-action" onClick={handleReset}>
            Subscribe another email
          </button>
        </div>
      )}

      {state === 'already_subscribed' && (
        <div className="state-message info" role="status">
          <div className="state-message-title">Already registered</div>
          <div>{responseMessage}</div>
          <button type="button" className="reset-action" onClick={handleReset}>
            Update or check another address
          </button>
        </div>
      )}

      {state === 'error' && (
        <div className="state-message error" role="alert">
          <div className="state-message-title">Unable to subscribe</div>
          <div>{responseMessage}</div>
          <button type="button" className="reset-action" onClick={handleReset}>
            Try again
          </button>
        </div>
      )}

      {(state === 'idle' || state === 'submitting') && (
        <form onSubmit={handleSubmit} noValidate>
          {!skuPrefilled && (
            <div className="form-group">
              <label className="form-label" htmlFor="stockping-sku">
                Product SKU
              </label>
              <input
                id="stockping-sku"
                type="text"
                className={`form-input ${fieldErrors.sku ? 'has-error' : ''}`}
                value={sku}
                onChange={(e) => setSku(e.target.value)}
                placeholder="e.g. HOOD-FLG-GRY-M"
                disabled={state === 'submitting'}
                autoComplete="off"
              />
              {fieldErrors.sku && <p className="field-error">{fieldErrors.sku}</p>}
            </div>
          )}

          <div className="form-group">
            <label className="form-label" htmlFor="stockping-email">
              Email Address
            </label>
            <input
              id="stockping-email"
              type="email"
              className={`form-input ${fieldErrors.email ? 'has-error' : ''}`}
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="name@example.com"
              disabled={state === 'submitting'}
              autoComplete="email"
            />
            {fieldErrors.email && <p className="field-error">{fieldErrors.email}</p>}
          </div>

          <button
            type="submit"
            className="submit-btn"
            disabled={state === 'submitting'}
          >
            {state === 'submitting' ? 'Subscribing...' : 'Notify Me When Available'}
          </button>
        </form>
      )}
    </div>
  );
};
