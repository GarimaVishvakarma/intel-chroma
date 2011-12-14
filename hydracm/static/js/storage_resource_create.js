
$(document).ready(function() {
  $('#storage_resource_create_dialog').dialog({
    autoOpen: false, modal: true, width: 'auto', maxHeight: 700, title: "Add storage device", resizable: false,
    buttons: {"Cancel": function() {$(this).dialog('close');}, "Add": function() {storage_resource_create_save();}},
    open: function() {
      $('.ui-dialog-buttonpane').
        find('button:first-child').button({
          icons: {
            primary: 'ui-icon-close'
          }
      }).next().button({
          icons: {
            primary: 'ui-icon-plus'
          }
      });
    }
  });

  $('.storage_resource_create_link').live('click', function(ev) {
    storage_resource_create();
    ev.stopPropagation();
  });

  $('#storage_resource_create_classes').change(function() {
    storage_resource_create_load_fields();
  });
});

function storage_resource_create_save()
{
  var selected = $('#storage_resource_create_classes option:selected').val()
  var tokens = selected.split(",")
  var module_name = tokens[0]
  var class_name = tokens[1]

  var attrs = new Object();
  $('#storage_resource_create_fields tr.field input').each(function() {
    var field_name = this.id.split("storage_resource_create_field_")[1];
    var field_value = $(this).attr('value');
    attrs[field_name] = field_value;
  });

  console.log(attrs);

  invoke_api_call(api_post, "storage_resource/", {'plugin': module_name, 'resource_class': class_name, 'attributes': attrs}, handlers = 
  {
    200 : function(data)
    {
      $('#storage_resource_create_dialog').dialog('close');
    }
  },
  error_callback = function(data){
  });
}

function storage_resource_create_load_fields()
{
  var selected = $('#storage_resource_create_classes option:selected').val()
  var tokens = selected.split(",")
  var module_name = tokens[0]
  var class_name = tokens[1]

  invoke_api_call(api_post, "storage_resource_class_fields/", {'plugin': module_name, 'resource_class': class_name},  handlers = 
  {
    200 : function(data)
    {
      $('#storage_resource_create_fields tr.field').remove();
      var row_markup = "";
      $.each(data.response, function(i, field_info)
      {
        row_markup += "<tr class='field'><th>" + field_info.label + ":</th><td><input type='entry' id='storage_resource_create_field_" + field_info.name + "'></input></td>";
        if (field_info.optional) {
          row_markup += "<td class='field_info'>Optional</td>"
        } else {
          row_markup += "<td class='field_info'></td>"
        }
        row_markup += "</tr>"
      });
      $('#storage_resource_create_fields').append(row_markup);
      $('#storage_resource_create_save').attr('disabled', false);
    }
  },
  error_callback = function(data){
  });
}

function storage_resource_create() {
  $('#storage_resource_create_save').attr('disabled', true);
  console.log('opening');
  $('#storage_resource_create_dialog').dialog('open');

  invoke_api_call(api_get, "creatable_storage_resource_classes", "", 
  success_callback = function(data)  
  {
    var option_markup = ""
    $.each(data.response, function(i, class_info) {
      option_markup += "<option value='" + class_info.plugin + "," + class_info.resource_class + "'>" + class_info.plugin + "-" + class_info.resource_class + "</option>"
    });
    $('#storage_resource_create_classes').html(option_markup);
    storage_resource_create_load_fields();
  },
  error_callback = function(data){
  });
}

