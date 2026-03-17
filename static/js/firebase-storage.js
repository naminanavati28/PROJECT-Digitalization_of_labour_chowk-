/**
 * Firebase Storage - Aadhaar image upload
 */
(function () {
  'use strict';

  window.FirebaseStorageHelper = {
    config: null,

    init: function (firebaseConfig) {
      this.config = firebaseConfig;
      if (!firebaseConfig || !firebaseConfig.apiKey) return false;
      if (typeof firebase === 'undefined') return false;
      if (!firebase.apps.length) firebase.initializeApp(firebaseConfig);
      return true;
    },

    uploadAadhaar: function (file, phone) {
      if (typeof firebase === 'undefined') {
        return Promise.reject(new Error('Firebase not loaded'));
      }
      var storage = firebase.storage();
      var filename = phone + '_' + Date.now() + '_aadhaar.jpg';
      var ref = storage.ref('aadhaar/' + filename);
      return ref.put(file)
        .then(function (snapshot) {
          return snapshot.ref.getDownloadURL();
        });
    }
  };
})();
