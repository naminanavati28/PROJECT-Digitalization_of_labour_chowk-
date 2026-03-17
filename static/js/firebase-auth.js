/**
 * Firebase Auth (OTP) integration for Kaam Bharat
 * Uses Firebase Phone Auth - multi-device compatible
 */
(function () {
  'use strict';

  window.FirebaseAuthHelper = {
    config: null,
    confirmationResult: null,

    init: function (firebaseConfig) {
      this.config = firebaseConfig;
      if (!firebaseConfig || !firebaseConfig.apiKey) {
        console.error('Firebase config missing');
        return false;
      }
      if (typeof firebase === 'undefined') {
        console.error('Firebase SDK not loaded');
        return false;
      }
      firebase.initializeApp(firebaseConfig);
      return true;
    },

    sendOTP: function (phoneNumber, recaptchaContainerId) {
      var self = this;
      var auth = firebase.auth();
      var container = document.getElementById(recaptchaContainerId || 'recaptcha-container');

      if (!container) {
        container = document.createElement('div');
        container.id = 'recaptcha-container';
        container.style.cssText = 'margin: 12px 0; min-height: 40px;';
        document.body.appendChild(container);
      }

      var verifier = new firebase.auth.RecaptchaVerifier(container.id, {
        size: 'invisible',
        callback: function () { }
      });

      var formattedPhone = phoneNumber.replace(/^0/, '+91').replace(/^(\d{10})$/, '+91$1');
      if (!formattedPhone.startsWith('+')) formattedPhone = '+91' + formattedPhone;

      return auth.signInWithPhoneNumber(formattedPhone, verifier)
        .then(function (confirmation) {
          self.confirmationResult = confirmation;
          return { success: true };
        })
        .catch(function (err) {
          console.error('Firebase send OTP error:', err);
          verifier.clear();
          return { success: false, error: err.message || 'Failed to send OTP' };
        });
    },

    verifyOTP: function (code) {
      if (!this.confirmationResult) {
        return Promise.resolve({ success: false, error: 'Session expired. Please try again.' });
      }
      return this.confirmationResult.confirm(code)
        .then(function (user) {
          return user.getIdToken().then(function (token) {
            return { success: true, idToken: token };
          });
        })
        .catch(function (err) {
          console.error('Firebase verify OTP error:', err);
          return { success: false, error: err.message || 'Invalid OTP' };
        });
    },

    getIdToken: function () {
      var user = firebase.auth().currentUser;
      if (!user) return Promise.resolve(null);
      return user.getIdToken();
    }
  };
})();
