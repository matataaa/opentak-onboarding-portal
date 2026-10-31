import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import './ChangePassword.css';

// Import the centralized API helper
import { authAPI } from '../services/api';
import { isValidNewPassword } from '../utils/passwordRules';

export default function ChangePassword() {
  const { t } = useTranslation();
  const navigate = useNavigate();

  const [formData, setFormData] = useState({
    currentPassword: '',
    newPassword: '',
    confirmPassword: ''
  });
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  // Change password mutation
  const changeMutation = useMutation({
    mutationFn: ({ currentPassword, newPassword }) =>
      authAPI.changePassword(currentPassword, newPassword),
    onSuccess: () => {
      setSuccess(t('profile.passwordChanged'));
      setError('');
      setFormData({
        currentPassword: '',
        newPassword: '',
        confirmPassword: ''
      });
      setTimeout(() => {
        navigate('/');
      }, 1500);
    },
    onError: (error) => {
      setError(error.response?.status === 403
        ? t('profile.currentPasswordIncorrect')
        : t('profile.changePasswordFailed'));
      setSuccess('');
    }
  });

  const handleChange = (e) => {
    setFormData({
      ...formData,
      [e.target.name]: e.target.value
    });
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    setError('');
    setSuccess('');

    // Validation
    if (!formData.currentPassword || !formData.newPassword || !formData.confirmPassword) {
      setError(t('profile.allFieldsRequired'));
      return;
    }

    if (formData.newPassword.length < 8) {
      setError(t('profile.newPasswordTooShort'));
      return;
    }

    // Check for disallowed characters
    if (!isValidNewPassword(formData.newPassword)) {
      setError(t('profile.passwordDisallowedChars'));
      return;
    }

    if (formData.newPassword !== formData.confirmPassword) {
      setError(t('profile.newPasswordsNoMatch'));
      return;
    }

    changeMutation.mutate({
      currentPassword: formData.currentPassword,
      newPassword: formData.newPassword
    });
  };

  const handleCancel = () => {
    navigate('/');
  };

  return (
    <div className="change-password-container">
      <div className="change-password-card">
        <h1>{t('profile.changePasswordTitle')}</h1>

        {error && <div className="error-message">{error}</div>}
        {success && <div className="success-message">{success}</div>}

        <form onSubmit={handleSubmit} className="change-password-form">
          <div className="form-group">
            <label htmlFor="currentPassword">{t('profile.currentPasswordLabel')}</label>
            <input
              type="password"
              id="currentPassword"
              name="currentPassword"
              value={formData.currentPassword}
              onChange={handleChange}
              required
              autoComplete="current-password"
            />
          </div>

          <div className="form-group">
            <label htmlFor="newPassword">{t('profile.newPasswordLabel')} *</label>
            <input
              type="password"
              id="newPassword"
              name="newPassword"
              value={formData.newPassword}
              onChange={handleChange}
              required
              autoComplete="new-password"
              minLength={8}
            />
            <small className="field-hint">{t('profile.passwordHint')}</small>
          </div>

          <div className="form-group">
            <label htmlFor="confirmPassword">{t('profile.confirmNewPasswordLabel')} *</label>
            <input
              type="password"
              id="confirmPassword"
              name="confirmPassword"
              value={formData.confirmPassword}
              onChange={handleChange}
              required
              autoComplete="new-password"
              minLength={8}
            />
          </div>

          <div className="form-actions">
            <button
              type="button"
              className="btn-cancel"
              onClick={handleCancel}
              disabled={changeMutation.isPending}
            >
              {t('common.cancel')}
            </button>
            <button
              type="submit"
              className="btn-submit"
              disabled={changeMutation.isPending}
            >
              {changeMutation.isPending ? t('profile.changingPassword') : t('profile.changePasswordButton')}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
