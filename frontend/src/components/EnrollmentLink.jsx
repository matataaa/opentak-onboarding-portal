import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import './EnrollmentLink.css';

export default function EnrollmentLink({ value }) {
  const { t } = useTranslation();
  const inputRef = useRef(null);
  const [copied, setCopied] = useState(false);

  if (!value) return null;

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API unavailable (non-HTTPS/denied): select the text so it can be copied manually
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  };

  return (
    <div className="enrollment-link">
      <p className="enrollment-link-hint">{t('eventsShared.sameDeviceHint')}</p>
      {value.startsWith('tak://') && (
        <a className="btn btn-primary" href={value}>{t('eventsShared.openInAtak')}</a>
      )}
      <input
        ref={inputRef}
        type="text"
        readOnly
        value={value}
        aria-label={t('eventsShared.linkLabel')}
        onFocus={(e) => e.target.select()}
      />
      <button type="button" className="btn btn-secondary" onClick={handleCopy}>
        {copied ? t('eventsShared.copied') : t('eventsShared.copyEnrollment')}
      </button>
    </div>
  );
}
