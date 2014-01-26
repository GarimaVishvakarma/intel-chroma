//
// INTEL CONFIDENTIAL
//
// Copyright 2013 Intel Corporation All Rights Reserved.
//
// The source code contained or described herein and all documents related
// to the source code ("Material") are owned by Intel Corporation or its
// suppliers or licensors. Title to the Material remains with Intel Corporation
// or its suppliers and licensors. The Material contains trade secrets and
// proprietary and confidential information of Intel or its suppliers and
// licensors. The Material is protected by worldwide copyright and trade secret
// laws and treaty provisions. No part of the Material may be used, copied,
// reproduced, modified, published, uploaded, posted, transmitted, distributed,
// or disclosed in any way without Intel's prior express written permission.
//
// No license under any patent, copyright, trade secret or other intellectual
// property right is granted to or conferred upon you by disclosure or delivery
// of the Materials, either expressly, by implication, inducement, estoppel or
// otherwise. Any license under such intellectual property rights must be
// express and approved by Intel in writing.


(function () {
  'use strict';

  angular.module('primus', [])
    .value('Primus', window.Primus)
    .factory('primus', ['Primus', 'BASE', 'disconnectModal', '$rootScope', primusFactory]);

  function primusFactory(Primus, BASE, disconnectModal, $rootScope) {
    var primus, modal;

    /**
     * Returns a new connection. or the existing one if get was already called.
     * @param {string} [namespace]
     */
    return function get () {
      if (primus) return primus;

      primus = new Primus(BASE + ':8888');

      primus.on('reconnecting', $applyFunc(function onReconnecting() {
        if (!modal)
          modal = disconnectModal();
      }));

      primus.on('open', $applyFunc(function onOpen() {
        if (modal) {
          modal.close();
          modal = null;
        }
      }));

      return primus;
    };

    function $applyFunc(func) {
      return function () {
        $rootScope.$apply(func);
      };
    }
  }
}());