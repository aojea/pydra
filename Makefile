PROTO_OUT_DIR  := pydra/core/generated
PROTO_SRC_DIR  := $(PROTO_OUT_DIR)/_proto_src

PLUGINREG_PROTO_URL := https://raw.githubusercontent.com/kubernetes/kubernetes/master/staging/src/k8s.io/kubelet/pkg/apis/pluginregistration/v1/api.proto
DRA_PROTO_URL       := https://raw.githubusercontent.com/kubernetes/kubernetes/master/staging/src/k8s.io/kubelet/pkg/apis/dra/v1beta1/api.proto

.PHONY: protos clean

protos:
	@echo "==> Downloading Kubernetes proto files..."
	mkdir -p $(PROTO_SRC_DIR)/pluginregistration/v1
	mkdir -p $(PROTO_SRC_DIR)/dra/v1beta1
	curl -sSfL $(PLUGINREG_PROTO_URL) -o $(PROTO_SRC_DIR)/pluginregistration/v1/api.proto
	curl -sSfL $(DRA_PROTO_URL)       -o $(PROTO_SRC_DIR)/dra/v1beta1/api.proto

	@echo "==> Compiling proto files..."
	mkdir -p $(PROTO_OUT_DIR)/pluginregistration/v1
	mkdir -p $(PROTO_OUT_DIR)/dra/v1beta1
	python -m grpc_tools.protoc \
		-I$(PROTO_SRC_DIR) \
		--python_out=$(PROTO_OUT_DIR) \
		--grpc_python_out=$(PROTO_OUT_DIR) \
		$(PROTO_SRC_DIR)/pluginregistration/v1/api.proto \
		$(PROTO_SRC_DIR)/dra/v1beta1/api.proto

	@echo "==> Creating __init__.py files..."
	touch $(PROTO_OUT_DIR)/__init__.py
	touch $(PROTO_OUT_DIR)/pluginregistration/__init__.py
	touch $(PROTO_OUT_DIR)/pluginregistration/v1/__init__.py
	touch $(PROTO_OUT_DIR)/dra/__init__.py
	touch $(PROTO_OUT_DIR)/dra/v1beta1/__init__.py

	@echo "==> Proto compilation complete."

clean:
	rm -rf $(PROTO_SRC_DIR)
	rm -f $(PROTO_OUT_DIR)/pluginregistration/v1/api_pb2*.py
	rm -f $(PROTO_OUT_DIR)/dra/v1beta1/api_pb2*.py
