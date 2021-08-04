// Copyright (c) 2021 MIT
//
// Permission to use, copy, modify, and distribute this software for any
// purpose with or without fee is hereby granted, provided that the above
// copyright notice and this permission notice appear in all copies.
//
// THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR(S) DISCLAIM ALL WARRANTIES
// WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
// MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL AUTHORS BE LIABLE FOR
// ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
// WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
// ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
// OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

#include <torch/script.h>
#include <torch/torch.h>
#include "json.hpp"
#include "runtime.h"
#include "runnableModule.h"
#include "logger.h"
#include "utils.h"
#include "communication.h"

using torch::autograd::Variable;
using torch::autograd::AutogradContext;
using torch::autograd::variable_list;

////////////////////////////////////////////////
// TsrXferFunc
////////////////////////////////////////////////
Variable
TsrXferFunc::forward(AutogradContext* ctx, Variable x, TsrXfer* xfer)
{
  ctx->saved_data["xfer"] = reinterpret_cast<int64_t>(xfer);

  if (xfer->type == TsrXfer::Send) {
    std::vector<torch::Tensor> splittedTsrs =
        x.split_with_sizes(xfer->splitSizes, xfer->splitCatDim);
    assert(splittedTsrs.size() == xfer->xferTagAndRank.size() + 1);
    size_t i;
    for (i = 0; i < xfer->xferTagAndRank.size(); ++i) {
      Tag tag = xfer->xferTagAndRank[i].first;
      Rank dest = xfer->xferTagAndRank[i].second;
      torch::Tensor tsr = splittedTsrs[i];
      DP_LOG(DEBUG, "Sending tag:%d to R:%d with %s", tag, dest,
          tsrToStr(tsr).c_str());
      xfer->commHandler->send(tsr, tag, dest, /*async*/ false);
    }
    return splittedTsrs[i];
  }
  else if (xfer->type == TsrXfer::Recv) {
    std::vector<int64_t> inputSizes = x.sizes().vec();
    std::vector<torch::Tensor> tsrList;
    size_t i;
    for (i = 0; i < xfer->xferTagAndRank.size(); ++i) {
      Tag tag = xfer->xferTagAndRank[i].first;
      Rank src = xfer->xferTagAndRank[i].second;
      inputSizes[xfer->splitCatDim] = xfer->splitSizes[i];
      torch::Tensor tsr = torch::empty(inputSizes);
      tsr.to(xfer->commHandler->getDev());
      DP_LOG(DEBUG, "Receiving tag:%d from R:%d with tensor: %s", tag, src,
          tsr.toString().c_str());
      xfer->commHandler->recv(tsr, tag, src, /*async*/ false);
      tsrList.push_back(tsr);
    }
    tsrList.push_back(x);
    return torch::cat(tsrList, xfer->splitCatDim);
  } else {
    DP_LOG(ERROR, "xfer type is %d, which is not supported.", xfer->type);
    return x;
  }
}

variable_list
TsrXferFunc::backward(AutogradContext* ctx, variable_list grad_output)
{
  TsrXfer* xfer = reinterpret_cast<TsrXfer*>(ctx->saved_data["xfer"].toInt());
  Variable x = grad_output[0];

  if (xfer->type == TsrXfer::Recv) {
    std::vector<torch::Tensor> splittedTsrs =
        x.split_with_sizes(xfer->splitSizes, xfer->splitCatDim);
    assert(splittedTsrs.size() == xfer->xferTagAndRank.size() + 1);
    size_t i;
    for (i = 0; i < xfer->xferTagAndRankBack.size(); ++i) {
      Tag tag = xfer->xferTagAndRankBack[i].first;
      Rank dest = xfer->xferTagAndRankBack[i].second;
      torch::Tensor tsr = splittedTsrs[i];
      DP_LOG(DEBUG, "Sending tag:%d to R:%d with %s", tag, dest,
          tsrToStr(tsr).c_str());
      xfer->commHandler->send(tsr, tag, dest, /*async*/ false);
    }
    return { splittedTsrs[i] };
  }
  else if (xfer->type == TsrXfer::Send) {
    std::vector<int64_t> inputSizes = x.sizes().vec();
    std::vector<torch::Tensor> tsrList;
    size_t i;
    for (i = 0; i < xfer->xferTagAndRankBack.size(); ++i) {
      Tag tag = xfer->xferTagAndRankBack[i].first;
      Rank src = xfer->xferTagAndRankBack[i].second;
      inputSizes[xfer->splitCatDim] = xfer->splitSizes[i];
      torch::Tensor tsr = torch::empty(inputSizes);
      tsr.to(xfer->commHandler->getDev());
      DP_LOG(DEBUG, "Receiving tag:%d from R:%d with tensor: %s", tag, src,
          tsr.toString().c_str());
      xfer->commHandler->recv(tsr, tag, src, /*async*/ false);
      tsrList.push_back(tsr);
    }
    tsrList.push_back(x);
    return { torch::cat(tsrList, xfer->splitCatDim) };
  }
  else {
    DP_LOG(ERROR, "xfer type is %d, which is not supported.", xfer->type);
    return grad_output;
  }
}


/**
 * Constructs RunnableModule
 */
RunnableModule::RunnableModule(RuntimeContext* rtctx,
                               json spec,
                               CommunicationHandler* commHandler,
                               c10::Device device)
  : rtctx(rtctx)
  , rank(spec["rank"].get<int>())
  , globalBatchSize(spec["globalBatchSize"].get<int>())
  , moduleList()
  , layersInJson(spec["layers"])
  , initialBatchSize(layersInJson[0]["config"][0])
  , commHandler(commHandler)
  , device(device)
  // , leavesForBackward()
  // , fpCtx(layersInJson)
  , layers()
  , layerQ()
  , fpInput()
  , fpOutput()
  , fpLoss()
{
  DP_LOG(DEBUG, "Constructing runnable module.. rank:%d", rank);
  DP_LOG(DEBUG, "             initialBatchSize:%d", initialBatchSize);
  DP_LOG(DEBUG, "             layersInJson's size:%d (from spec)", static_cast<int>(spec["layers"].size()));
  DP_LOG(DEBUG, "             layersInJson's size:%d", static_cast<int>(layersInJson.size()));
  
  // It's important to reserve the same, so that layers won't get copied over
  // to another address.. (layer's are pointing each other with raw pointer.)
  layers.reserve(layersInJson.size());

  for (auto& ldsc : layersInJson) {
    int id = ldsc["id"].get<int>();
    std::string name = ldsc["name"].get<std::string>();
    std::string moduleLoc = ldsc["moduleSavedLocation"].get<std::string>();
    DP_LOG(DEBUG, " %d-th layer's name: %s, moduleLoc: %s", id, name.c_str(),
        moduleLoc.c_str());
    
    torch::jit::Module module = torch::jit::load(std::string(rtctx->homedir) +
        "/DeepPoolRuntime/" + moduleLoc);
    DP_LOG(DEBUG, " layer's module is loaded.");
    if (name == "concat") {
      DP_LOG(DEBUG, " layer is concat.");
    } else {
      DP_LOG(DEBUG, " layer is not concat.");
    }

    module.to(device);
    module.train();
    DP_LOG(DEBUG, " layer's module is moved to device and set for train mode.");

    int layerLocalBatch = ldsc["config"][0].get<int>();
    bool layerIsActive = layerLocalBatch > 0;
    bool detachInput = true;
    if (name == "ReLU2d" || name == "ReLU1d") {
      detachInput = false;
    }
    std::vector<Layer*> prevLayers;
    for (auto& plidjson : ldsc["prevLayers"]) {
      int plid = plidjson.get<int>();
      prevLayers.push_back(&layers[plid]);
    }
    layers.emplace_back(module, id, layerIsActive, detachInput, prevLayers);
    DP_LOG(DEBUG, " layer's module is moved to device and set for train mode.");

    // Communications.
    if (layerIsActive && ldsc.contains("tensorTx")) {
      std::map<int, std::vector<json> > sendListDict;
      for (auto& item : ldsc["tensorTx"]) {
        int nextLayerId = item["prop"]["nextLayerId"].get<int>();
        if (sendListDict.find(nextLayerId) == sendListDict.end()) {
          sendListDict[nextLayerId] = std::vector<json>();
        }
        sendListDict[nextLayerId].push_back(item);
      }
      for (const auto& kv : sendListDict) {
        const std::vector<json>& sendList = kv.second;

        TsrXfer xfer(commHandler);
        xfer.type = TsrXfer::Send;
        xfer.splitCatDim = 0; // Sample dimension.
        int xferSampleSum = 0;
        for (const json& item : sendList) {
          int xferSamples = item["prop"]["xferSamples"].get<int>();
          xfer.splitSizes.push_back(xferSamples);
          xferSampleSum += xferSamples;

          auto xferName = item["name"].get<std::string>();
          Tag tag = commHandler->getTag(xferName);
          Tag tagB = commHandler->getTag(xferName + "_back");
          Rank dest = item["dest"].get<Rank>();
          xfer.xferTagAndRank.push_back(std::make_pair(tag, dest));
          xfer.xferTagAndRankBack.push_back(std::make_pair(tagB, dest));
        }
        int remainder = ldsc["outputDim"][xfer.splitCatDim].get<int>() - xferSampleSum;
        DP_LOG(DEBUG, "remainder: %d, sum: %d", remainder, xferSampleSum);
        xfer.splitSizes.push_back(remainder);
        layers.end()->xferOuts.push_back(std::move(xfer));
      }
    }

    if (layerIsActive && ldsc.contains("tensorRx")) {
      TsrXfer xfer(commHandler);
      xfer.type = TsrXfer::Recv;
      xfer.splitCatDim = 0;
      int xferSampleSum = 0;
      for (const json& item : ldsc["tensorRx"]) {
        int xferSamples = item["prop"]["xferSamples"].get<int>();
        xfer.splitSizes.push_back(xferSamples);
        xferSampleSum += xferSamples;

        auto xferName = item["name"].get<std::string>();
        Tag tag = commHandler->getTag(xferName);
        Tag tagB = commHandler->getTag(xferName + "_back");
        Rank src = item["src"].get<Rank>();
        xfer.xferTagAndRank.push_back(std::make_pair(tag, src));
        xfer.xferTagAndRankBack.push_back(std::make_pair(tagB, src));
      }
      int remainder = ldsc["inputDim"][xfer.splitCatDim].get<int>() - xferSampleSum;
      DP_LOG(DEBUG, "remainder: %d, sum: %d", remainder, xferSampleSum);
      xfer.splitSizes.push_back(remainder);
      layers.end()->xferIns.push_back(std::move(xfer));
    }

    // std::string moduleName = format("%d:%s", ldsc["id"].get<int>(), name);
    // register_module(moduleName, module);
    // DP_LOG(DEBUG, "registered module as a submodule.");
    
    // std::shared_ptr<torch::nn::Module> modulePtr(std::move(module));
    moduleList.push_back(module);
    DP_LOG(DEBUG, " layer's module is pushed back.");
    DP_LOG(DEBUG, " id: %d and moduleListsize: %d", id, (int)moduleList.size());
    assert(id + 1 == (int)moduleList.size());
  }
}

/**
 * Dumps the entire model parameters into the given vector.
 */
void
RunnableModule::getParameters(std::vector<torch::Tensor>* parameters)
{
  for (const auto& module : moduleList) {
    for (const auto& params : module.parameters()) {
      parameters->push_back(params);
    }
  }
}

/**
 * Initiate an iteration.
 */
void
RunnableModule::iterInit(torch::Tensor x)
{
  layerQ.clear();
  layerQ.push_back(&layers[0]);
  fpInput = x.to(device);
  fpOutput.reset();
  fpLoss.reset();
}

/**
 * Execute a forward pass of this model.
 * 
 * \return Returns true if forward pass is completed.
 */
bool
RunnableModule::forwardAStep()
{
  DP_LOG(DEBUG, "layerQ size: %d", (int)layerQ.size());
  Layer* layer = layerQ.front();
  layerQ.pop_front();

  // TODO: potentially we can make track if the cuda kernel is finished
  // or probably finished.
  bool skipSinceNotReady = false;
  if (layer->status == LayerStatus::PENDING_BP) {
    DIE("%d-th layer is processed again.", layer->id);
  }
  DP_LOG(DEBUG, "lid:%d.", layer->id);

  torch::Tensor input;
  if (layer->prevLayers.size() == 0) {
    input = fpInput; // TODO: cleanup..
  } else if (layer->prevLayers.size() == 1) {
    input = layer->prevLayers[0]->output; //.detach_();
    if (layer->detachInput) {
      layer->detachedInput = input.detach();
      layer->detachedInput.requires_grad_();
      input = layer->detachedInput;
    }
  } else {
    DIE("%d-th layer has more than 2 previous layers (except concat), which"
        " is not supported.", layer->id);
  }
  // else if (ldsc["name"].get<std::string>() == std::string("concat")) {
  //   DIE("%d-th layer is concat, which is not implemented.", lid);
  //   //TODO: implement.
  //   // skipSinceNotReady should be updated here.
  // } 

  DP_LOG(DEBUG, "input:%s.", input.toString().c_str());

  if (skipSinceNotReady) {
    return false;
  }

  if (layer->active) {
    DP_LOG(DEBUG, "Layer %d is active.", layer->id);
    if (!input.defined()) {
      DP_LOG(DEBUG, "input is not defined. Using empty tensor.");
      input = torch::empty(0);
      input = input.to(device, /*non_blocking*/ true, /*copy*/ false);
    }

    std::vector<torch::jit::IValue> inputVec;
    inputVec.push_back(input);
    DP_LOG(DEBUG, "inputVec is prepared.");
    layer->output = layer->module.forward(inputVec).toTensor();
    DP_LOG(DEBUG, "module.forward called.");

    // auto h = layer->output.register_hook([layer](torch::Tensor grad){
    //   DP_LOG(DEBUG, "lid:%d grad: %s", layer->id, tsrToStr(grad).c_str());
    // });

    // fpCtx.fpTensorToReturn = output;
    // fpCtx.runCriterionAndLoss = true;
    // DP_LOG(DEBUG, "return values are set.");

  } else { // This rank doesn't participate for this layer.
    DP_LOG(DEBUG, "Layer %d is not active.", layer->id);
    // output = torch::empty(0);
    // output.to(device, /*non_blocking*/ true, /*copy*/ false);
    // fpCtx.runCriterionAndLoss = false;
    // fpCtx.fpTensorToReturn.reset();
  }
  
  layer->status = LayerStatus::PENDING_BP;
  
  for (auto& nextLayerPtr : layer->nextLayers) {
    if (nextLayerPtr->status == LayerStatus::PENDING_FP) {
      layerQ.push_back(nextLayerPtr);
      DP_LOG(DEBUG, "nextLayer %d is queued for processing.", nextLayerPtr->id);
    } else {
      DP_LOG(DEBUG, "nextLayer %d is already processed.", nextLayerPtr->id);
    }
  }

  // Forward pass is completed.
  if (layerQ.empty()) {
    DP_LOG(DEBUG, "no more layers to process.");
    if (layer->output.defined()) {
      fpOutput = layer->output; // TODO: clean up.
      // runCriterionAndLoss = true;
    } else {
      fpOutput.reset();
      // runCriterionAndLoss = false;
    }
    return true;
  }
  return false;
}

/**
 * Compute the loss from forward pass.
 */
void
RunnableModule::loss(torch::Tensor targets)
{
  if (fpOutput.defined()) {
    DP_LOG(DEBUG, "fpOutput: %s", tsrToStr(fpOutput).c_str());
    // auto fpOutputD = fpOutput.detach();
    // DP_LOG(DEBUG, "fpOutputD: %s", tsrToStr(fpOutputD).c_str());
    
    fpLoss = torch::nll_loss(fpOutput, targets);
    DP_LOG(DEBUG, "fpLoss: %s", tsrToStr(fpLoss).c_str());
    fpLoss.backward();
    DP_LOG(DEBUG, "fpLoss.backward() done. ");
  }
}

/**
 * Execute a backward pass of this model.
 * 
 * \return Returns true if backward pass is completed.
 */
bool
RunnableModule::backwardAStep()
{
  DP_LOG(DEBUG, "layerQ size: %d", (int)layerQ.size());
  Layer* layer = layerQ.front();
  layerQ.pop_front();

  // TODO: potentially we can make track if the cuda kernel is finished
  // or probably finished.
  if (layer->status == LayerStatus::PENDING_FP) {
    DIE("%d-th layer is processed again.", layer->id);
  }
  DP_LOG(DEBUG, "lid:%d.", layer->id);

  bool shouldInvokeBackward = layer->nextLayers.size() > 0;
  std::vector<torch::Tensor> gradInputs;
  if (layer->nextLayers.size() == 0) {
    DP_LOG(DEBUG, "No nextLayers.");
  } else if (layer->nextLayers.size() >= 1) {
    for (auto nextLayerPtr : layer->nextLayers) {
      if (nextLayerPtr->detachInput) {
        gradInputs.push_back(nextLayerPtr->detachedInput.grad());
        // DP_LOG(DEBUG, "nextLayer detachedInput? %d, added to gradInputs: %s",
        //     nextLayerPtr->detachInput,
        //     tsrToStr(nextLayerPtr->detachedInput.grad()).c_str());
        shouldInvokeBackward = shouldInvokeBackward && nextLayerPtr->detachInput;
        // WARNING! this is a bit hacky... it assumes all children layers detach or not
        // together.
      }
    }
    // gradInputs.push_back(layer->nextLayers[0]->gradOut);
  }
  // else if (ldsc["name"].get<std::string>() == std::string("concat")) {
  //   DIE("%d-th layer is concat, which is not implemented.", lid);
  //   //TODO: implement.
  //   // skipSinceNotReady should be updated here.
  // } 

  DP_LOG(DEBUG, "gradInputs.size(): %d", (int)gradInputs.size());

  if (layer->active) {
    DP_LOG(DEBUG, "Layer %d is active.", layer->id);
    // if (!input.defined()) {
    //   DP_LOG(DEBUG, "input is not defined. Using empty tensor.");
    //   gradIn = torch::empty(0);
    //   gradIn = gradIn.to(device, /*non_blocking*/ true, /*copy*/ false);
    // }

    if (shouldInvokeBackward) {
      for (auto gradIn : gradInputs) {
        DP_LOG(DEBUG, "output:%s gradIn:%s", 
            layer->output.toString().c_str(), gradIn.toString().c_str());
        layer->output.backward(gradIn);
        DP_LOG(DEBUG, "layer->output.backward is called.");
      }
    } else {
      // This layer must have only one following in-place layer.
    }
  } else { // This rank doesn't participate for this layer.
    DP_LOG(DEBUG, "Layer %d is not active.", layer->id);
  }
  
  layer->status = LayerStatus::PENDING_FP;
  
  for (auto& prevLayerPtr : layer->prevLayers) {
    if (prevLayerPtr->status == LayerStatus::PENDING_BP) {
      layerQ.push_back(prevLayerPtr);
    } else {
      DP_LOG(DEBUG, "prevLayer %d is already processed.", prevLayerPtr->id);
    }
  }

  // Forward pass is completed.
  if (layerQ.empty()) {
    DP_LOG(DEBUG, "no more layers to process.");
    return true;
  }
  return false;
}
